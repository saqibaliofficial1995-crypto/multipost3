"""Instagram Reels upload (Instagram API with Instagram Login).

Uses graph.instagram.com directly without any Facebook Page.
"""
import os
import time
import requests

GRAPH = "https://graph.instagram.com"


def upload(cfg, public_video_url, caption):
    ig = cfg.get("instagram", {})
    user_id = os.environ.get("IG_USER_ID") or ig.get("ig_user_id")
    token = os.environ.get("IG_ACCESS_TOKEN")

    if not token:
        raise RuntimeError("IG_ACCESS_TOKEN missing. Set it in environment or via setup_wizard.py")
    if not user_id:
        raise RuntimeError("IG_USER_ID missing. Set it in environment or config.yaml")

    if not public_video_url:
        raise RuntimeError(
            "Instagram ke liye public_url chahiye. Sidecar .json me "
            '"public_url" field do, ya hosting mode setup karo.'
        )

    # 1) Container banao
    r = requests.post(
        f"{GRAPH}/{user_id}/media",
        data={
            "media_type": "REELS",
            "video_url": public_video_url,
            "caption": caption[:2200],
            "share_to_feed": "true",
            "access_token": token,
        },
        timeout=120,
    )
    r.raise_for_status()
    container_id = r.json()["id"]
    print(f"[ig] container: {container_id}")

    # 2) FINISHED hone tak wait karo
    for attempt in range(90):
        time.sleep(5)
        s = requests.get(
            f"{GRAPH}/{container_id}",
            params={"fields": "status_code,status", "access_token": token},
            timeout=60,
        )
        s.raise_for_status()
        code = s.json().get("status_code")
        if code == "FINISHED":
            break
        if code == "ERROR":
            raise RuntimeError(f"IG container error: {s.json().get('status')}")
        print(f"[ig] processing... ({code})")
    else:
        raise RuntimeError("IG container did not finish within timeout")

    # 3) Publish
    p = requests.post(
        f"{GRAPH}/{user_id}/media_publish",
        data={"creation_id": container_id, "access_token": token},
        timeout=120,
    )
    p.raise_for_status()
    media_id = p.json()["id"]

    permalink = ""
    try:
        pl = requests.get(
            f"{GRAPH}/{media_id}",
            params={"fields": "permalink", "access_token": token},
            timeout=60,
        )
        permalink = pl.json().get("permalink", "")
    except Exception:
        pass

    print(f"[ig] published: {media_id}")
    return media_id, permalink
