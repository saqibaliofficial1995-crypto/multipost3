"""Public GitHub Releases media host for Instagram Reels.

Instagram Graph API requires a publicly reachable direct video URL.
This module uploads normalized MP4s to the public repository `multipost-media`
as Release assets, returning the unauthenticated download URL, and cleans up
the release and asset afterwards so the repo never accumulates storage.
"""
import datetime as dt
import os
import secrets
import requests


def _get_credentials(token=None, owner=None, repo=None):
    t = token or os.environ.get("MEDIA_REPO_TOKEN") or os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    o = owner or os.environ.get("MEDIA_REPO_OWNER") or os.environ.get("GITHUB_REPOSITORY_OWNER") or "saqibaliofficial1995-crypto"
    r = repo or os.environ.get("MEDIA_REPO", "multipost-media")
    return t, o, r


def _headers(token):
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def upload_media(local_file_path, token=None, owner=None, repo=None):
    """Uploads normalized MP4 to a public GitHub Release and returns the direct download URL.
    
    Returns:
        tuple: (download_url, release_info)
    """
    tok, own, rep = _get_credentials(token, owner, repo)
    if not tok:
        raise RuntimeError("No GitHub token found for media hosting. Set MEDIA_REPO_TOKEN or GH_TOKEN.")

    if not os.path.exists(local_file_path):
        raise FileNotFoundError(f"Video file not found at {local_file_path}")

    now_str = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d%H%M%S")
    rand_suffix = secrets.token_hex(4)
    tag_name = f"reel-{now_str}-{rand_suffix}"
    api_base = f"https://api.github.com/repos/{own}/{rep}"

    # 1. Create a release
    rel_payload = {
        "tag_name": tag_name,
        "name": f"reel-media-{now_str}",
        "draft": False,
        "prerelease": False,
    }
    r = requests.post(f"{api_base}/releases", headers=_headers(tok), json=rel_payload, timeout=30)
    if r.status_code != 201:
        print(f"[host_media] Release creation error (HTTP {r.status_code}): {r.text}")
        r.raise_for_status()

    rel_data = r.json()
    release_id = rel_data["id"]
    upload_url_tmpl = rel_data["upload_url"]
    upload_url = upload_url_tmpl.split("{")[0]

    # 2. Upload video asset
    file_name = os.path.basename(local_file_path)
    file_size = os.path.getsize(local_file_path)

    asset_headers = _headers(tok)
    asset_headers["Content-Type"] = "video/mp4"
    asset_headers["Content-Length"] = str(file_size)

    with open(local_file_path, "rb") as f:
        file_bytes = f.read()

    u_resp = requests.post(
        upload_url,
        headers=asset_headers,
        params={"name": file_name},
        data=file_bytes,
        timeout=180,
    )
    if u_resp.status_code != 201:
        print(f"[host_media] Asset upload error (HTTP {u_resp.status_code}): {u_resp.text}")
        # Clean up the empty release before raising
        try:
            requests.delete(f"{api_base}/releases/{release_id}", headers=_headers(tok), timeout=15)
        except Exception:
            pass
        u_resp.raise_for_status()

    asset_data = u_resp.json()
    download_url = asset_data.get("browser_download_url")
    asset_id = asset_data.get("id")

    # 3. Verify public direct download URL without auth headers
    v_resp = requests.get(download_url, headers={"Range": "bytes=0-1024"}, timeout=30, allow_redirects=True)
    if v_resp.status_code not in (200, 206):
        raise RuntimeError(f"Direct media download URL failed verification: HTTP {v_resp.status_code}")

    print(f"[host_media] Uploaded video asset to {own}/{rep} ({download_url})")

    release_info = {
        "owner": own,
        "repo": rep,
        "release_id": release_id,
        "tag_name": tag_name,
        "asset_id": asset_id,
    }
    return download_url, release_info


def delete_media(release_info, token=None):
    """Removes the release asset, release, and git tag reference after publishing."""
    if not release_info:
        return

    tok, _, _ = _get_credentials(token)
    if not tok:
        print("[host_media] Warning: Cannot delete media release, missing GitHub token.")
        return

    own = release_info["owner"]
    rep = release_info["repo"]
    rel_id = release_info["release_id"]
    tag = release_info["tag_name"]
    asset_id = release_info.get("asset_id")
    api_base = f"https://api.github.com/repos/{own}/{rep}"

    # 1. Delete asset if present
    if asset_id:
        try:
            requests.delete(f"{api_base}/releases/assets/{asset_id}", headers=_headers(tok), timeout=20)
        except Exception as e:
            print(f"[host_media] Notice: Could not delete asset {asset_id} ({e})")

    # 2. Delete release
    try:
        requests.delete(f"{api_base}/releases/{rel_id}", headers=_headers(tok), timeout=20)
    except Exception as e:
        print(f"[host_media] Notice: Could not delete release {rel_id} ({e})")

    # 3. Delete tag ref
    try:
        requests.delete(f"{api_base}/git/refs/tags/{tag}", headers=_headers(tok), timeout=20)
    except Exception as e:
        print(f"[host_media] Notice: Could not delete tag ref {tag} ({e})")

    print(f"[host_media] Deleted temporary release asset and tag '{tag}' from {own}/{rep}.")
