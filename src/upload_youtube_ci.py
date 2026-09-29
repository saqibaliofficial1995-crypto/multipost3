"""YouTube upload for CI (GitHub Actions) — no browser, refresh token se auth."""
import os
import random
import time

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

from auth_env import credentials_from_env

RETRIABLE = {500, 502, 503, 504}


def upload(cfg, video_path, title, description, tags):
    yt_cfg = cfg["youtube"]
    creds = credentials_from_env()
    youtube = build("youtube", "v3", credentials=creds, cache_discovery=False)

    body = {
        "snippet": {
            "title": title[:100],
            "description": description[:4900],
            "tags": (tags or []) + yt_cfg.get("default_tags", []),
            "categoryId": str(yt_cfg.get("category_id", "22")),
        },
        "status": {
            # views ke liye ye 2 fields critical hain
            "privacyStatus": yt_cfg.get("privacy_status", "public"),
            "selfDeclaredMadeForKids": bool(yt_cfg.get("made_for_kids", False)),
        },
    }

    media = MediaFileUpload(video_path, chunksize=8 * 1024 * 1024,
                            resumable=True, mimetype="video/*")
    request = youtube.videos().insert(part="snippet,status", body=body,
                                      media_body=media)

    response, error, retry = None, None, 0
    while response is None:
        try:
            status, response = request.next_chunk()
            if status:
                print(f"[yt] {int(status.progress() * 100)}%", flush=True)
        except HttpError as e:
            if e.resp.status in RETRIABLE:
                error = e
            else:
                raise
        except (OSError, IOError) as e:
            error = e

        if error:
            retry += 1
            if retry > 6:
                raise RuntimeError(f"YouTube upload failed: {error}")
            sleep = random.random() * (2 ** retry)
            print(f"[yt] retry {retry} in {sleep:.1f}s", flush=True)
            time.sleep(sleep)
            error = None

    video_id = response["id"]

    # thumbnail: video ke saath same-name .jpg (repo me ho to)
    for ext in (".jpg", ".jpeg", ".png"):
        thumb = os.path.splitext(video_path)[0] + ext
        if os.path.exists(thumb):
            try:
                youtube.thumbnails().set(videoId=video_id,
                                         media_body=thumb).execute()
                print("[yt] thumbnail set")
            except HttpError as e:
                print(f"[yt] thumbnail skip: {e}")
            break

    # Read back actual visibility status
    try:
        check_resp = youtube.videos().list(part="status", id=video_id).execute()
        items = check_resp.get("items", [])
        if items:
            actual_privacy = items[0].get("status", {}).get("privacyStatus", "")
            print(f"[yt] actual privacy status: {actual_privacy}")
            if actual_privacy == "private" and yt_cfg.get("privacy_status", "public") == "public":
                print("\n[ALERT] YouTube locked this video to private! Your Google Cloud API project requires a Google compliance audit to publish videos publicly via API.\n")
    except Exception as e:
        print(f"[yt] notice: could not verify post-upload privacy status ({e})")

    return video_id, "https://www.youtube.com/watch?v=" + video_id
