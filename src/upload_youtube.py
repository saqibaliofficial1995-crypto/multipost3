"""YouTube upload — resumable, retry-safe, Shorts-safe metadata."""
import os
import random
import time

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

from auth_google import get_credentials

RETRIABLE = {500, 502, 503, 504}


def upload(cfg, video_path, title, description, tags):
    yt_cfg = cfg["youtube"]
    creds = get_credentials(yt_cfg["client_secret_file"], yt_cfg["token_file"])
    youtube = build("youtube", "v3", credentials=creds, cache_discovery=False)

    body = {
        "snippet": {
            "title": title[:100],
            "description": description[:4900],
            "tags": (tags or []) + yt_cfg.get("default_tags", []),
            "categoryId": str(yt_cfg.get("category_id", "22")),
        },
        "status": {
            # === YE 2 FIELDS SABSE ZAROORI HAIN ===
            # privacyStatus chhoot gaya to video private reh jaati hai -> 0 views
            "privacyStatus": yt_cfg.get("privacy_status", "public"),
            # madeForKids true hua to recommendations band -> reach ~0
            "selfDeclaredMadeForKids": bool(yt_cfg.get("made_for_kids", False)),
        },
    }

    media = MediaFileUpload(video_path, chunksize=8 * 1024 * 1024, resumable=True,
                            mimetype="video/*")
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)

    response, error, retry = None, None, 0
    while response is None:
        try:
            status, response = request.next_chunk()
            if status:
                print(f"[yt] {int(status.progress() * 100)}%")
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
                raise RuntimeError(f"YouTube upload failed after retries: {error}")
            sleep = random.random() * (2 ** retry)
            print(f"[yt] retry {retry} in {sleep:.1f}s")
            time.sleep(sleep)
            error = None

    video_id = response["id"]
    print(f"[yt] uploaded: {video_id}")

    # Custom thumbnail (CTR ka sabse bada factor). Sidecar: video ke saath same
    # naam ki .jpg/.png rakho.
    for ext in (".jpg", ".jpeg", ".png"):
        thumb = os.path.splitext(video_path)[0] + ext
        if os.path.exists(thumb):
            try:
                youtube.thumbnails().set(videoId=video_id, media_body=thumb).execute()
                print("[yt] thumbnail set")
            except HttpError as e:
                print(f"[yt] thumbnail skip: {e}")
            break

    return video_id, "https://www.youtube.com/watch?v=" + video_id
