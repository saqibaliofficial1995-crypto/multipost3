"""GitHub Releases based temporary media host for Instagram Reels.

Public repo `multipost-media` hosts the video asset temporarily.
Instagram fetches via the unauthenticated GitHub release asset redirect URL.
The release and tag are immediately deleted after Instagram finishes processing.
"""
import datetime as dt
import os
import random
import string
import requests


def _headers(token):
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28"
    }


def create_release_and_upload(token, owner, repo, local_file_path):
    """Creates a temporary release, uploads video as asset, verifies unauthenticated 200,

    and returns (public_url, release_info).
    """
    now_str = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d%H%M%S")
    rand_str = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
    tag_name = f"media-{now_str}-{rand_str}"

    api_base = f"https://api.github.com/repos/{owner}/{repo}"

    # 1. Create release
    rel_payload = {
        "tag_name": tag_name,
        "name": f"temp-media-{now_str}",
        "draft": false if False else False,
        "prerelease": false if False else False
    }
    r = requests.post(f"{api_base}/releases", headers=_headers(token), json=rel_payload, timeout=30)
    r.raise_for_status()
    rel_data = r.json()
    release_id = rel_data["id"]
    upload_url_tmpl = rel_data["upload_url"]
    upload_url = upload_url_tmpl.split("{")[0]

    # 2. Upload asset
    file_name = os.path.basename(local_file_path)
    asset_headers = _headers(token)
    asset_headers["Content-Type"] = "video/mp4"

    with open(local_file_path, "rb") as f:
        file_bytes = f.read()

    u_resp = requests.post(
        upload_url,
        headers=asset_headers,
        params={"name": file_name},
        data=file_bytes,
        timeout=180
    )
    u_resp.raise_for_status()
    asset_data = u_resp.json()
    download_url = asset_data["browser_download_url"]

    # 3. Verify unauthenticated 200
    v_resp = requests.get(download_url, headers={"Range": "bytes=0-1024"}, timeout=30, allow_redirects=True)
    if v_resp.status_code not in (200, 206):
        raise RuntimeError(f"Direct media URL failed unauthenticated verification: HTTP {v_resp.status_code}")

    print(f"[media_host] Uploaded asset to public release: {download_url} (HTTP {v_resp.status_code} Verified)")

    release_info = {
        "owner": owner,
        "repo": repo,
        "release_id": release_id,
        "tag_name": tag_name,
        "asset_id": asset_data["id"]
    }
    return download_url, release_info


def cleanup_release(token, release_info):
    """Deletes the release and tag from GitHub."""
    owner = release_info["owner"]
    repo = release_info["repo"]
    rel_id = release_info["release_id"]
    tag = release_info["tag_name"]
    api_base = f"https://api.github.com/repos/{owner}/{repo}"

    try:
        # Delete release
        requests.delete(f"{api_base}/releases/{rel_id}", headers=_headers(token), timeout=30)
        # Delete tag ref
        requests.delete(f"{api_base}/git/refs/tags/{tag}", headers=_headers(token), timeout=30)
        print(f"[media_host] Cleaned up temporary release {rel_id} and tag {tag}")
    except Exception as e:
        print(f"[media_host] Notice: Cleanup failed ({e})")
