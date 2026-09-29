"""Google OAuth — 'ek baar login, hamesha chale' wala setup.

Aapki session-expire problem ka asli fix yahan hai:
  1. access_type='offline' + prompt='consent'  -> refresh token milta hai
  2. refresh token disk par save hota hai aur silently refresh hota hai
  3. Google Cloud me app ko PUBLISHED rakho, warna 'Testing' mode me
     refresh token sirf 7 din chalta hai (yahi "session khatam ho gaya" hai).
"""
import json
import os

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/youtube.upload",
          "https://www.googleapis.com/auth/youtube"]


def get_credentials(client_secret_file, token_file):
    creds = None
    if os.path.exists(token_file):
        try:
            creds = Credentials.from_authorized_user_file(token_file, SCOPES)
        except Exception:
            creds = None

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            _save(creds, token_file)
            return creds
        except Exception as e:
            print(f"[auth] refresh fail ({e}) -> dobara login chahiye")

    flow = InstalledAppFlow.from_client_secrets_file(client_secret_file, SCOPES)
    # access_type/prompt ke bina refresh token NAHI milta -> 1 ghante me expire
    creds = flow.run_local_server(
        port=0, access_type="offline", prompt="consent",
        authorization_prompt_message="Browser khul raha hai, wahan login karo: {url}",
    )
    if not creds.refresh_token:
        raise RuntimeError(
            "Refresh token nahi mila. Google account ke 3rd-party access se is app ko "
            "remove karo, phir dobara login karo."
        )
    _save(creds, token_file)
    return creds


def _save(creds, token_file):
    os.makedirs(os.path.dirname(os.path.abspath(token_file)), exist_ok=True)
    with open(token_file, "w") as f:
        f.write(creds.to_json())
    os.chmod(token_file, 0o600)
    print(f"[auth] token saved -> {token_file}")
