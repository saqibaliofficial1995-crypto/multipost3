"""Instagram Reels publishing client for CI using Instagram API with Instagram Login.

API Host: graph.instagram.com
Scopes: instagram_business_basic, instagram_business_content_publish
Endpoints:
- Token refresh: GET https://graph.instagram.com/refresh_access_token?grant_type=ig_refresh_token&access_token={token}
- Publishing limit: GET https://graph.instagram.com/{ig_user_id}/content_publishing_limit?fields=config,quota_usage
- Container creation: POST https://graph.instagram.com/{ig_user_id}/media
- Container status: GET https://graph.instagram.com/{container_id}?fields=status_code,status
- Publish container: POST https://graph.instagram.com/{ig_user_id}/media_publish
- Permalink: GET https://graph.instagram.com/{media_id}?fields=permalink

No Facebook Page or other endpoints are used.
"""
import os
import subprocess
import time
import requests

GRAPH_IG = "https://graph.instagram.com"


def is_available(cfg=None):
    """Checks whether Instagram credentials (IG_ACCESS_TOKEN and IG_USER_ID) are set."""
    token = os.environ.get("IG_ACCESS_TOKEN")
    user_id = os.environ.get("IG_USER_ID") or (cfg.get("instagram", {}).get("ig_user_id") if cfg else None)
    return bool(token and user_id)


def _handle_graph_error(response, context="Instagram API request"):
    """Parses Instagram API error responses and raises human-readable messages."""
    try:
        data = response.json()
        err = data.get("error", {})
        code = err.get("code")
        message = err.get("message", "Unknown error")
        subcode = err.get("error_subcode")

        if code == 190:
            raise RuntimeError(
                f"[ig] Instagram Access Token has expired or is invalid (Error 190, subcode {subcode}): {message}. "
                "Please run setup_wizard.py Step 4 to generate a new token."
            )
        elif code == 200:
            raise RuntimeError(
                f"[ig] Instagram permission error (Error 200): {message}. "
                "The token lacks required permissions (instagram_business_content_publish, instagram_business_basic)."
            )
        else:
            raise RuntimeError(f"[ig] {context} failed (Error {code}): {message}")
    except (ValueError, KeyError):
        response.raise_for_status()


def refresh_long_lived_token(token):
    """Refreshes a long-lived Instagram User Access Token and logs days until expiry.

    Calls GET https://graph.instagram.com/refresh_access_token?grant_type=ig_refresh_token&access_token={token}
    Tokens at least 24 hours old are refreshed for a new 60-day window.
    """
    if not token:
        return token

    url = f"{GRAPH_IG}/refresh_access_token"
    try:
        resp = requests.get(
            url,
            params={"grant_type": "ig_refresh_token", "access_token": token},
            timeout=30,
        )
        if resp.status_code == 200:
            data = resp.json()
            new_token = data.get("access_token", token)
            expires_in = data.get("expires_in", 5184000)
            days_left = round(expires_in / 86400, 1)
            print(f"[ig] Token successfully refreshed. Expiry in {days_left} days ({expires_in}s).")

            # If running in environment with GitHub CLI secret update permission, update secret
            gh_token = os.environ.get("MEDIA_REPO_TOKEN") or os.environ.get("GH_TOKEN")
            repo = os.environ.get("GITHUB_REPOSITORY") or "bjviufutu-dot/multipost"
            if gh_token and new_token != token:
                try:
                    subprocess.run(
                        ["gh", "secret", "set", "IG_ACCESS_TOKEN", "--repo", repo],
                        input=new_token.encode("utf-8"),
                        capture_output=True,
                        timeout=15,
                    )
                    print("[ig] Updated IG_ACCESS_TOKEN in GitHub repository secrets.")
                except Exception:
                    pass
            return new_token
        else:
            err = resp.json().get("error", {})
            # Meta returns error if token is less than 24 hours old, which is normal
            msg = err.get("message", "")
            print(f"[ig] Token refresh check: {msg or resp.status_code} (token active).")
    except Exception as e:
        print(f"[ig] Notice: Token refresh request failed ({e}). Proceeding with current token.")

    return token


def check_publishing_limit(ig_user_id, token):
    """Checks the rolling 24-hour content publishing limit.

    Endpoint: GET https://graph.instagram.com/{ig_user_id}/content_publishing_limit
    Returns:
        tuple: (allowed: bool, quota_usage: int, quota_total: int)
    """
    url = f"{GRAPH_IG}/{ig_user_id}/content_publishing_limit"
    r = requests.get(
        url,
        params={"fields": "config,quota_usage", "access_token": token},
        timeout=30,
    )
    if r.status_code != 200:
        _handle_graph_error(r, "Checking publishing limit")

    data = r.json().get("data", [{}])[0]
    quota_usage = data.get("quota_usage", 0)
    config = data.get("config", {})
    quota_total = config.get("quota_total", 25)

    print(f"[ig] Publishing quota: {quota_usage}/{quota_total} used in last 24 hours.")
    if quota_usage >= 25 or quota_usage >= quota_total:
        return False, quota_usage, quota_total
    return True, quota_usage, quota_total


def upload(cfg, public_video_url, caption):
    """Publishes a Reel to Instagram using the Instagram API with Instagram Login.

    Args:
        cfg (dict): Configuration dictionary.
        public_video_url (str): Directly downloadable URL for the video asset.
        caption (str): Caption text (truncated to 2200 characters).

    Returns:
        tuple: (media_id, permalink)
    """
    if not is_available(cfg):
        print("[ig] SKIPPED: IG_ACCESS_TOKEN and/or IG_USER_ID are unset.")
        return None, ""

    raw_token = os.environ.get("IG_ACCESS_TOKEN")
    user_id = os.environ.get("IG_USER_ID") or cfg.get("instagram", {}).get("ig_user_id")

    # 1. Automatic long-lived token refresh on every run
    token = refresh_long_lived_token(raw_token)

    # 2. Check 24-hour publishing limit
    try:
        allowed, quota_usage, quota_total = check_publishing_limit(user_id, token)
        if not allowed:
            print(f"[ig] Skipped: 24-hour publishing limit reached ({quota_usage}/{quota_total}).")
            return None, ""
    except Exception as e:
        print(f"[ig] Notice: Could not verify publishing limit: {e}")

    # 3. Create Reels container via POST https://graph.instagram.com/{ig_user_id}/media
    print(f"[ig] Creating Reel container for user {user_id}...")
    container_payload = {
        "media_type": "REELS",
        "video_url": public_video_url,
        "caption": (caption or "")[:2200],
        "share_to_feed": "true",
        "access_token": token,
    }
    r = requests.post(f"{GRAPH_IG}/{user_id}/media", data=container_payload, timeout=120)
    if r.status_code != 200:
        _handle_graph_error(r, "Creating Reel container")

    container_id = r.json().get("id")
    print(f"[ig] Reel container created: {container_id}")

    # 4. Poll status until FINISHED (up to 90 polls with 5s delay = 7.5 mins)
    for attempt in range(1, 91):
        time.sleep(5)
        poll_resp = requests.get(
            f"{GRAPH_IG}/{container_id}",
            params={"fields": "status_code,status", "access_token": token},
            timeout=60,
        )
        if poll_resp.status_code != 200:
            _handle_graph_error(poll_resp, f"Polling container status (poll {attempt})")

        poll_data = poll_resp.json()
        code = poll_data.get("status_code")
        status_desc = poll_data.get("status", "")
        print(f"[ig] Poll {attempt}/90: status_code={code} ({status_desc})", flush=True)

        if code == "FINISHED":
            break
        elif code == "ERROR":
            raise RuntimeError(f"[ig] Reel video processing failed in container: {status_desc}")
    else:
        raise TimeoutError(f"[ig] Reel container {container_id} did not reach FINISHED status within 450 seconds.")

    # 5. Publish media container via POST https://graph.instagram.com/{ig_user_id}/media_publish
    print(f"[ig] Publishing container {container_id}...")
    pub_resp = requests.post(
        f"{GRAPH_IG}/{user_id}/media_publish",
        data={"creation_id": container_id, "access_token": token},
        timeout=120,
    )
    if pub_resp.status_code != 200:
        _handle_graph_error(pub_resp, "Publishing Reel container")

    media_id = pub_resp.json().get("id")
    print(f"[ig] Reel published successfully with media ID: {media_id}")

    # 6. Fetch and log permalink via GET https://graph.instagram.com/{media_id}?fields=permalink
    permalink = ""
    try:
        pl_resp = requests.get(
            f"{GRAPH_IG}/{media_id}",
            params={"fields": "permalink", "access_token": token},
            timeout=60,
        )
        if pl_resp.status_code == 200:
            permalink = pl_resp.json().get("permalink", "")
            print(f"[ig] Reel permalink: {permalink}")
    except Exception as e:
        print(f"[ig] Notice: Could not fetch permalink: {e}")

    return media_id, permalink
