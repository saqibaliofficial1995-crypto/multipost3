"""MULTIPOST — ek video, teeno platform.

Flow:
  inbox/ scan  ->  SQLite queue  ->  ffmpeg normalize (9:16)
  ->  YouTube  ->  Facebook  ->  Instagram  ->  processed/ me move

Har platform ka status alag store hota hai, isliye agar IG fail hua to
YouTube dobara upload nahi hoga. Duplicate-safe.

Usage:
  python src/main.py --config config.yaml scan      # inbox se jobs banao
  python src/main.py --config config.yaml run        # ek batch upload karo
  python src/main.py --config config.yaml run --all  # jitne pending hain sab
  python src/main.py --config config.yaml status     # report
  python src/main.py --config config.yaml login      # sirf OAuth login karo
"""
import argparse
import datetime as dt
import json
import os
import shutil
import sys
import time
import traceback

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import db  # noqa: E402
import normalize  # noqa: E402

VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi"}


def load_cfg(path):
    with open(path) as f:
        cfg = yaml.safe_load(f)
    base = os.path.dirname(os.path.abspath(path))
    for k, v in cfg["paths"].items():
        cfg["paths"][k] = os.path.normpath(os.path.join(base, v))
    for section, key in (("youtube", "client_secret_file"), ("youtube", "token_file"),
                         ("facebook", "page_access_token_file"),
                         ("instagram", "page_access_token_file")):
        if section in cfg and key in cfg[section]:
            cfg[section][key] = os.path.normpath(
                os.path.join(base, cfg[section][key]))
    return cfg


def enabled_platforms(cfg):
    return [p for p in ("youtube", "facebook", "instagram")
            if cfg["platforms"].get(p)]


def read_sidecar(video_path):
    """video.mp4 ke saath video.json rakho:
    {"title": "...", "description": "...", "hashtags": "#a #b",
     "public_url": "https://.../video.mp4"}
    """
    side = os.path.splitext(video_path)[0] + ".json"
    if os.path.exists(side):
        with open(side) as f:
            return json.load(f)
    return {}


def cmd_scan(cfg):
    paths = cfg["paths"]
    os.makedirs(paths["inbox"], exist_ok=True)
    os.makedirs(paths["processed"], exist_ok=True)
    platforms = enabled_platforms(cfg)
    added = 0

    for name in sorted(os.listdir(paths["inbox"])):
        path = os.path.join(paths["inbox"], name)
        if not os.path.isfile(path):
            continue
        if os.path.splitext(name)[1].lower() not in VIDEO_EXT:
            continue

        meta = read_sidecar(path)
        title = meta.get("title") or os.path.splitext(name)[0].replace("_", " ")
        desc = meta.get("description") or title
        tags = meta.get("hashtags") or cfg["metadata"]["fallback_hashtags"]
        if cfg["metadata"].get("append_hashtags_to_description"):
            desc = f"{desc}\n\n{tags}"

        job_id = db.add_job(paths["db"], path, title, desc, tags,
                            meta.get("public_url"), platforms)
        if job_id:
            added += 1
            print(f"[scan] + {name} -> job {job_id} ({', '.join(platforms)})")

    print(f"[scan] {added} naye job add hue")
    return added


def ensure_normalized(cfg, row):
    if row["norm_path"] and os.path.exists(row["norm_path"]):
        return row["norm_path"]
    out = os.path.join(
        cfg["paths"]["work"],
        os.path.splitext(os.path.basename(row["src_path"]))[0] + "_vertical.mp4",
    )
    if not os.path.exists(out):
        print(f"[ffmpeg] normalize -> {os.path.basename(out)}")
        normalize.normalize(row["src_path"], out, cfg["encode"])
    db.set_norm_path(cfg["paths"]["db"], row["job_id"], out)
    return out


def do_upload(cfg, row, video):
    platform = row["platform"]
    if platform == "youtube":
        import upload_youtube
        return upload_youtube.upload(cfg, video, row["title"], row["description"],
                                     (row["hashtags"] or "").replace("#", "").split())
    if platform == "facebook":
        raise NotImplementedError("Facebook Page route is disabled. Only YouTube and Instagram are supported.")
    if platform == "instagram":
        import upload_instagram
        return upload_instagram.upload(cfg, row["public_url"], row["description"])
    raise ValueError(f"unknown platform {platform}")


def cmd_run(cfg, run_all=False):
    dbp = cfg["paths"]["db"]
    today = dt.date.today().isoformat()
    limit = cfg["schedule"]["max_uploads_per_day"]
    stagger = cfg["schedule"].get("stagger_minutes_between_platforms", 0) * 60
    done_now = 0

    while True:
        if db.daily_count(dbp, today) >= limit:
            print(f"[run] daily limit {limit} reached")
            break

        row = db.next_pending(dbp)
        if row is None:
            print("[run] kuch pending nahi")
            break

        print(f"\n=== {row['platform']} :: {row['title']} ===")
        try:
            video = ensure_normalized(cfg, row)
            remote_id, remote_url = do_upload(cfg, row, video)
            db.mark(dbp, row["id"], "done", remote_id, remote_url)
            db.bump_daily(dbp, today)
            done_now += 1
            print(f"[ok] {row['platform']} -> {remote_url}")
        except Exception as e:
            traceback.print_exc()
            db.mark(dbp, row["id"], "failed", error=str(e)[:800])
            print(f"[fail] {row['platform']}: {e}")

        if db.job_fully_done(dbp, row["job_id"]):
            dest = os.path.join(cfg["paths"]["processed"],
                                os.path.basename(row["src_path"]))
            try:
                shutil.move(row["src_path"], dest)
                print(f"[move] processed/{os.path.basename(dest)}")
            except Exception as e:
                print(f"[move] skip: {e}")

        if not run_all:
            break
        if stagger:
            print(f"[run] {stagger // 60} min wait (rate-limit safety)")
            time.sleep(stagger)

    print(f"\n[run] {done_now} upload complete")
    return done_now


def cmd_status(cfg):
    rows = db.report(cfg["paths"]["db"])
    if not rows:
        print("koi record nahi")
        return
    print(f"{'PLATFORM':<11}{'STATUS':<9}TITLE")
    print("-" * 70)
    for r in rows:
        print(f"{r['platform']:<11}{r['status']:<9}{(r['title'] or '')[:40]}")
        if r["remote_url"]:
            print(f"           -> {r['remote_url']}")
        if r["error"]:
            print(f"           !! {r['error'][:100]}")


def cmd_login(cfg):
    import auth_google
    auth_google.get_credentials(cfg["youtube"]["client_secret_file"],
                               cfg["youtube"]["token_file"])
    print("YouTube login OK. Ab token refresh apne aap hoga.")


def main():
    ap = argparse.ArgumentParser(description="Multipost: 1 video -> YT + FB + IG")
    ap.add_argument("command", choices=["scan", "run", "status", "login"])
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--all", action="store_true", help="saare pending process karo")
    args = ap.parse_args()

    cfg = load_cfg(args.config)
    for p in ("work", "processed", "tokens"):
        os.makedirs(cfg["paths"][p], exist_ok=True)
    db.init(cfg["paths"]["db"])

    if args.command == "scan":
        cmd_scan(cfg)
    elif args.command == "run":
        cmd_scan(cfg)
        cmd_run(cfg, run_all=args.all)
    elif args.command == "status":
        cmd_status(cfg)
    elif args.command == "login":
        cmd_login(cfg)


if __name__ == "__main__":
    main()
