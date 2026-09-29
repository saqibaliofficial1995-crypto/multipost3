"""CI/cloud ke liye headless Google auth.

GitHub Actions me browser nahi hota, isliye interactive login possible nahi.
Solution: ek baar local machine par login karo, refresh token nikaalo, aur usko
GitHub Secret me daal do. Refresh token expire nahi hota (agar app PUBLISHED ho).

Refresh token nikalne ka tareeka:
    python src/main.py --config config.yaml login
    python src/print_refresh_token.py
"""
import os

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube",
    "https://www.googleapis.com/auth/drive",
]


def credentials_from_env(channel=1):
    suffix = f"_{channel}" if str(channel) == "2" else ""
    client_id = os.environ.get(f"YT_CLIENT_ID{suffix}")
    client_secret = os.environ.get(f"YT_CLIENT_SECRET{suffix}")
    refresh_token = os.environ.get(f"YT_REFRESH_TOKEN{suffix}")

    if not (client_id and client_secret and refresh_token):
        token_file = f"tokens/yt_token{suffix}.json" if suffix else "tokens/yt_token.json"
        if os.path.exists(token_file):
            creds = Credentials.from_authorized_user_file(token_file, SCOPES)
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            return creds

    missing = [
        k for k, v in {
            f"YT_CLIENT_ID{suffix}": client_id,
            f"YT_CLIENT_SECRET{suffix}": client_secret,
            f"YT_REFRESH_TOKEN{suffix}": refresh_token,
        }.items() if not v
    ]
    if missing:
        raise RuntimeError(
            f"YouTube secrets missing{f' for channel {channel}' if suffix else ''}: " + ", ".join(missing) +
            "\nGitHub repo -> Settings -> Secrets and variables -> Actions me daalo."
        )

    creds = Credentials(
        token=None,
        refresh_token=refresh_token,
        client_id=client_id,
        client_secret=client_secret,
        token_uri="https://oauth2.googleapis.com/token",
        scopes=SCOPES,
    )
    # Har run par fresh access token le lo (refresh token se, bina login ke)
    creds.refresh(Request())
    return creds
def ig_access_token():
    token = os.environ.get("IG_ACCESS_TOKEN")
    if not token:
        raise RuntimeError("IG_ACCESS_TOKEN secret missing")
    return token

