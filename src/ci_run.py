"""CLOUD RUNNER for Multipost.

Runs in GitHub Actions (or locally).
- Drive is the only source: fetches oldest pending video from Google Drive 'Multipost Inbox'.
- Normalizes to vertical 1080x1920, 30fps, <=180s.
- Publishes to YouTube (public, madeForKids: false).
- Publishes to Instagram Reels (using temporary GitHub Releases asset hosting).
- Cleanly skips Instagram if IG_ACCESS_TOKEN and IG_USER_ID are unset.
- On success: moves Drive file to 'Uploaded' subfolder and updates state/uploaded.json.
- On failure: leaves Drive file alone for next retry.
"""
import argparse
import datetime as dt
import json
import os
import shutil
import socket
import sys
import traceback
import urllib3.util.connection

# Force IPv4 to prevent TLS handshakes hanging on networks with broken IPv6
try:
    urllib3.util.connection.allowed_gai_family = lambda: socket.AF_INET
except Exception:
    pass

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import auth_env
import drive_source
import github_media_host
import host_media
import normalize
import storage
import upload_instagram_ci
import upload_youtube_ci


class Log:
    """Writes to both console and log file."""
    def __init__(self, path):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.f = open(path, "a", encoding="utf-8")
        self.write(f"\n===== RUN {dt.datetime.now(dt.timezone.utc).isoformat()}Z =====")

    def write(self, msg):
        print(msg, flush=True)
        self.f.write(str(msg) + "\n")
        self.f.flush()

    def close(self):
        self.f.close()


def load_cfg(path):
    with open(path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    cfg["_base"] = os.path.dirname(os.path.abspath(path))
    return cfg


def is_instagram_enabled(cfg):
    platforms = cfg.get("platforms", {})
    if "instagram" in platforms and not platforms["instagram"]:
        return False
    ig_cfg = cfg.get("instagram", {})
    if "enabled" in ig_cfg and not ig_cfg["enabled"]:
        return False
    return True


def load_state(path):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            try:
                data = json.load(f)
                if isinstance(data, dict):
                    data.setdefault("items", {})
                    data.setdefault("history", [])
                    return data
            except Exception:
                pass
    return {"items": {}, "history": []}


def save_state(path, state):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)


def get_uploaded_ids(state):
    """Returns set of drive_file_ids that are confirmed published to YouTube."""
    uploaded = set()
    for fid, item in state.get("items", {}).items():
        if isinstance(item, dict) and (item.get("youtube_video_id") or item.get("uploaded_at_utc")):
            uploaded.add(fid)
    for item in state.get("history", []):
        fid = item.get("drive_file_id")
        if fid and (item.get("youtube_video_id") or item.get("uploaded_at_utc")):
            uploaded.add(fid)
    return uploaded


def run(cfg, max_uploads=1, dry_run=False, channel=None, config_path=None):
    log = Log(cfg["paths"].get("log", "state/run.log"))
    state_file = cfg["paths"].get("state", "state/uploaded.json")
    state = load_state(state_file)
    work_dir = cfg["paths"].get("work", "work")
    os.makedirs(work_dir, exist_ok=True)

    log.write(f"[start] max_uploads={max_uploads}, dry_run={dry_run}")

    # 1. Google Auth & Drive session
    target_channel = channel or cfg.get("channel") or (2 if ("2" in os.path.basename(config_path or "")) else 1)
    ch_suffix = f"_{target_channel}" if str(target_channel) == "2" else ""
    try:
        creds = auth_env.credentials_from_env(channel=target_channel)
        drive_session = drive_source.get_drive_session(creds.token)
    except Exception as e:
        log.write(f"[fatal] Google authentication failed (channel {target_channel}): {e}")
        log.close()
        return 1

    inbox_id = (
        os.environ.get(f"DRIVE_FOLDER_ID{ch_suffix}")
        or os.environ.get("DRIVE_FOLDER_ID")
        or cfg.get("drive", {}).get("inbox_folder_id")
    )
    if not inbox_id:
        log.write("[fatal] DRIVE_FOLDER_ID is missing from environment and config.")
        log.close()
        return 1

    # 2. Get pending videos from Google Drive
    uploaded_ids = get_uploaded_ids(state)
    try:
        pending_videos = drive_source.list_pending_videos(drive_session, inbox_id, uploaded_ids)
    except Exception as e:
        log.write(f"[fatal] Failed listing Drive inbox files: {e}")
        log.close()
        return 1

    log.write(f"[drive] Found {len(pending_videos)} unuploaded video(s) in Drive Inbox.")
    if not pending_videos:
        log.write("[finish] Nothing to upload. Inbox is clear.")
        log.close()
        return 0

    max_attempts = cfg.get("schedule", {}).get("max_attempts", 3)
    eligible_videos = []
    any_error = False

    for vid_info in pending_videos:
        fid = vid_info["id"]
        fname = vid_info["name"]
        item_state = state.get("items", {}).get(fid, {})
        attempts = item_state.get("attempts", 0)
        if attempts >= max_attempts:
            last_err = item_state.get("last_error", "Unknown error")
            log.write(f"[fail] Video '{fname}' ({fid}) reached max attempts ({attempts}/{max_attempts}). Skipping: {last_err}")
            any_error = True
        else:
            eligible_videos.append(vid_info)

    if not eligible_videos:
        log.write(f"[finish] No eligible videos to process (all pending videos reached max {max_attempts} attempts).")
        log.close()
        return 1 if any_error else 0

    to_process = eligible_videos[:max_uploads]
    uploaded_folder_id = None
    if not dry_run:
        try:
            uploaded_folder_id = drive_source.get_or_create_uploaded_folder(drive_session, inbox_id)
        except Exception as e:
            log.write(f"[warn] Could not get/create 'Uploaded' subfolder: {e}")

    processed_count = 0

    for vid_info in to_process:
        file_id = vid_info["id"]
        file_name = vid_info["name"]
        created_time = vid_info.get("createdTime", "")
        log.write(f"\n-------- Processing: {file_name} (ID: {file_id}, Created: {created_time}) --------")

        item_state = state["items"].setdefault(file_id, {})
        attempts = item_state.get("attempts", 0)
        if attempts > 0:
            log.write(f"[retry] Attempt {attempts + 1}/{max_attempts} for '{file_name}' (prior error: {item_state.get('last_error', 'none')})")

        # Download video & optional sidecar txt
        local_video = os.path.join(work_dir, file_name)
        txt_id = None
        sidecar_text = None
        try:
            log.write(f"[drive] Downloading {file_name}...")
            drive_source.download_file(drive_session, file_id, local_video)
            txt_id, sidecar_text = drive_source.get_sidecar_txt(drive_session, inbox_id, file_name)
            if txt_id:
                log.write(f"[drive] Found metadata sidecar file for {file_name}")
        except Exception as e:
            log.write(f"[fail] Drive download error: {e}")
            item_state["attempts"] = attempts + 1
            item_state["last_error"] = str(e)[:400]
            save_state(state_file, state)
            any_error = True
            continue

        # Normalization (ffmpeg 1080x1920, 30fps, <=180s)
        norm_video = os.path.join(work_dir, f"norm_{file_id}.mp4")
        try:
            if cfg.get("encode", {}).get("enabled", True):
                log.write("[ffmpeg] Normalizing to 1080x1920 30fps...")
                normalize.normalize(local_video, norm_video, cfg["encode"])
                log.write("[ffmpeg] Normalization complete.")
            else:
                norm_video = local_video
        except Exception as e:
            log.write(f"[fail] Video normalization failed: {e}")
            item_state["attempts"] = attempts + 1
            item_state["last_error"] = str(e)[:400]
            save_state(state_file, state)
            any_error = True
            continue

        # Probe video duration from normalized video
        duration = 0
        try:
            p_info = normalize.probe(norm_video)
            duration = p_info.get("duration", 0)
        except Exception as e:
            log.write(f"[warn] Could not probe video duration ({e})")

        # Metadata extraction (Sidecar .txt wins, else AI Gemini video analysis, else fallbacks)
        import llm_meta
        if sidecar_text:
            meta = drive_source.extract_metadata(file_name, sidecar_text=sidecar_text)
            path_used = "sidecar_txt"
        else:
            channel_topic = cfg.get("metadata", {}).get("channel_topic", "")
            if not channel_topic and norm_video and os.path.exists(norm_video):
                log.write("[topic] No channel_topic configured. Gemini is watching the video to infer the channel topic...")
                channel_topic = llm_meta.infer_topic_from_video(norm_video)
                log.write(f"[topic] Automatically inferred channel topic from video visual content: '{channel_topic}'")
                cfg.setdefault("metadata", {})["channel_topic"] = channel_topic
                if config_path and os.path.exists(config_path):
                    try:
                        with open(config_path, "w", encoding="utf-8") as cf:
                            yaml.dump(cfg, cf, default_flow_style=False, sort_keys=False)
                        log.write(f"[topic] Saved inferred topic to {config_path}")
                    except Exception as e:
                        log.write(f"[warn] Could not save inferred topic to {config_path}: {e}")
            default_desc = cfg.get("metadata", {}).get("default_description", "")
            default_tags = cfg.get("metadata", {}).get("default_tags", ["shorts", "viral"])
            meta = llm_meta.generate_metadata(
                video_path=norm_video,
                filename=file_name,
                duration=duration,
                channel_topic=channel_topic,
                default_desc=default_desc,
                default_tags=default_tags
            )
            path_used = meta.get("path_used", "topic_default_fallback")

        log.write(f"[meta] Path used: {path_used}")
        if meta.get("model_used"):
            log.write(f"[meta] Model used: {meta['model_used']}")
        title = meta["title"]
        desc = meta["description"]
        tags = meta["tags"]
        hashtags = meta.get("hashtags", [])

        log.write(f"[meta] Title ({len(title)} chars): {title}")
        log.write(f"[meta] Description:\n{desc}")
        tags_str = ",".join(tags)
        log.write(f"[meta] Tags ({len(tags)} items, {len(tags_str)} chars): {tags}")
        log.write(f"[meta] Hashtags: {hashtags}")

        # Safety Check 1: Reject duplicate title if already exists in state/uploaded.json
        existing_titles = {item.get("title", "").strip().lower() for item in state.get("items", {}).values() if item.get("title")}
        existing_titles.update(item.get("title", "").strip().lower() for item in state.get("history", []) if item.get("title"))
        if title.strip().lower() in existing_titles:
            log.write(f"[reject] Safety block: Title '{title}' already exists in state/uploaded.json. Duplicate rejected.")
            if not dry_run:
                item_state["attempts"] = attempts + 1
                item_state["last_error"] = f"Duplicate title rejected: {title}"
                save_state(state_file, state)
                continue

        # Safety Check 2: Block live publishing unless path is video_ai_gemini (or manual sidecar_txt)
        if not dry_run and path_used not in ("video_ai_gemini", "sidecar_txt"):
            log.write(f"[block] Safety block: Publishing blocked because metadata path is '{path_used}'. Only 'video_ai_gemini' (or sidecar .txt) is allowed to publish live.")
            item_state["attempts"] = attempts + 1
            item_state["last_error"] = f"Publishing blocked: {path_used} not allowed for live publish"
            save_state(state_file, state)
            continue

        if dry_run:
            log.write(f"[dry-run] Successfully generated and verified metadata for {file_name} ({file_id}).")
            if not is_instagram_enabled(cfg):
                log.write("[ig] disabled by config.")
            elif not upload_instagram_ci.is_available(cfg):
                log.write("[dry-run] [ig] SKIPPED: IG_ACCESS_TOKEN and/or IG_USER_ID are unset.")
            for p in [local_video, norm_video]:
                if os.path.exists(p):
                    try:
                        os.remove(p)
                    except Exception:
                        pass
            processed_count += 1
            continue

        # YouTube Upload (with state check to allow Instagram retries without duplicate YouTube uploads)
        yt_id = item_state.get("youtube_video_id")
        yt_url = item_state.get("youtube_url", "")
        if yt_id:
            log.write(f"[yt] Already published to YouTube ({yt_url}). Skipping re-upload.")
        else:
            try:
                log.write("[yt] Uploading to YouTube...")
                yt_id, yt_url = upload_youtube_ci.upload(cfg, norm_video, title, desc, tags)
                log.write(f"[yt] Published successfully: {yt_url}")
                item_state["youtube_video_id"] = yt_id
                item_state["youtube_url"] = yt_url
            except Exception as e:
                log.write(f"[fail] YouTube upload failed: {e}")
                item_state["attempts"] = attempts + 1
                item_state["last_error"] = str(e)[:400]
                save_state(state_file, state)
                any_error = True
                continue

        # Instagram Upload (via GitHub Releases temporary asset host or S3)
        ig_id = item_state.get("instagram_id")
        ig_permalink = item_state.get("instagram_permalink", "")
        if not is_instagram_enabled(cfg):
            log.write("[ig] disabled by config.")
        elif ig_id:
            log.write(f"[ig] Already published to Instagram ({ig_permalink}). Skipping.")
        elif upload_instagram_ci.is_available(cfg):
            media_token = os.environ.get("MEDIA_REPO_TOKEN") or os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
            media_owner = os.environ.get("MEDIA_REPO_OWNER") or os.environ.get("GITHUB_REPOSITORY_OWNER") or "bjviufutu-dot"
            media_repo = os.environ.get("MEDIA_REPO", "multipost-media")

            public_url = None
            rel_info = None

            try:
                if media_token and media_owner:
                    log.write(f"[ig] Hosting video via temporary release on {media_owner}/{media_repo}...")
                    public_url, rel_info = host_media.upload_media(
                        norm_video, token=media_token, owner=media_owner, repo=media_repo
                    )
                elif storage.available():
                    log.write("[ig] Hosting video via S3/R2 storage...")
                    key = f"multipost/vertical/{file_id}.mp4"
                    public_url = storage.upload_public(norm_video, key)

                if public_url:
                    log.write("[ig] Publishing Reel to Instagram...")
                    caption = f"{title}\n\n{desc}" if desc else title
                    ig_id, ig_permalink = upload_instagram_ci.upload(cfg, public_url, caption)
                    if ig_id:
                        log.write(f"[ig] Published successfully: {ig_permalink}")
                        item_state["instagram_id"] = ig_id
                        item_state["instagram_permalink"] = ig_permalink
                else:
                    log.write("[ig] SKIPPED: No media host configured (set MEDIA_REPO_TOKEN/GH_TOKEN or S3 secrets).")
            except Exception as e:
                log.write(f"[warn] Instagram publish error: {e}")
                item_state["instagram_error"] = str(e)[:400]
            finally:
                if rel_info:
                    host_media.delete_media(rel_info, token=media_token)
        else:
            log.write("[ig] SKIPPED: IG_ACCESS_TOKEN and/or IG_USER_ID are unset.")

        # Move Drive file to 'Uploaded' subfolder on success
        if uploaded_folder_id:
            try:
                drive_source.move_file_to_uploaded(drive_session, file_id, inbox_id, uploaded_folder_id)
                log.write(f"[drive] Moved {file_name} to 'Uploaded' subfolder.")
                if txt_id:
                    drive_source.move_file_to_uploaded(drive_session, txt_id, inbox_id, uploaded_folder_id)
            except Exception as e:
                log.write(f"[warn] Could not move Drive file to Uploaded folder: {e}")

        # Update state/uploaded.json
        now_utc = dt.datetime.now(dt.timezone.utc).isoformat() + "Z"
        record = {
            "drive_file_id": file_id,
            "filename": file_name,
            "title": title,
            "youtube_video_id": yt_id,
            "youtube_url": yt_url,
            "instagram_id": ig_id,
            "instagram_permalink": ig_permalink,
            "uploaded_at_utc": now_utc
        }
        state["items"][file_id] = record
        state["history"].append(record)
        save_state(state_file, state)
        log.write(f"[state] Recorded {file_id} in state/uploaded.json")

        # Cleanup local work files
        for p in [local_video, norm_video]:
            if os.path.exists(p):
                try:
                    os.remove(p)
                except Exception:
                    pass

        processed_count += 1

    log.write(f"\n[finish] Processed {processed_count} video(s).")
    log.close()
    return 1 if any_error else 0


def main():
    parser = argparse.ArgumentParser(description="Multipost Cloud Runner")
    parser.add_argument("--config", default="config.ci.yaml", help="Path to YAML config")
    parser.add_argument("--max-uploads", type=int, default=None, help="Max videos to process this run")
    parser.add_argument("--dry-run", action="store_true", help="Test run without uploading")
    parser.add_argument("--channel", type=int, default=None, choices=[1, 2], help="Target channel (1 or 2)")
    args = parser.parse_args()

    cfg = load_cfg(args.config)
    env_max = os.environ.get("MAX_UPLOADS")
    max_uploads = args.max_uploads if args.max_uploads is not None else (int(env_max) if env_max else 1)
    env_dry = os.environ.get("DRY_RUN", "false").lower() in ("true", "1", "yes")
    dry_run = args.dry_run or env_dry

    code = run(cfg, max_uploads=max_uploads, dry_run=dry_run)
    sys.exit(code)


if __name__ == "__main__":
    main()
