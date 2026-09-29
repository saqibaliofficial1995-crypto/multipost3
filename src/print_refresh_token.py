"""Local machine par ek baar chalao — GitHub Secrets ke liye values print karta hai.

    python src/main.py --config config.yaml login      # pehle login
    python src/print_refresh_token.py                  # phir ye

Output ko GitHub -> Settings -> Secrets and variables -> Actions me daal do.
"""
import json
import os
import sys

TOKEN_FILE = os.environ.get("YT_TOKEN_FILE", "tokens/yt_token.json")
CLIENT_FILE = os.environ.get(
    "YT_CLIENT_FILE", "tokens/google_client_secret.json")


def main():
    if not os.path.exists(TOKEN_FILE):
        sys.exit(f"{TOKEN_FILE} nahi mila. Pehle 'login' command chalao.")

    with open(TOKEN_FILE) as f:
        tok = json.load(f)

    refresh = tok.get("refresh_token")
    client_id = tok.get("client_id")
    client_secret = tok.get("client_secret")

    if not (client_id and client_secret) and os.path.exists(CLIENT_FILE):
        with open(CLIENT_FILE) as f:
            data = json.load(f)
        blob = data.get("installed") or data.get("web") or {}
        client_id = client_id or blob.get("client_id")
        client_secret = client_secret or blob.get("client_secret")

    if not refresh:
        sys.exit(
            "Refresh token nahi mila.\n"
            "Fix: Google account -> Security -> Third-party access -> is app ko "
            "remove karo, phir 'login' dobara chalao."
        )

    print("\n=== GitHub Secrets me ye 3 values daalo ===\n")
    print(f"YT_CLIENT_ID\n{client_id}\n")
    print(f"YT_CLIENT_SECRET\n{client_secret}\n")
    print(f"YT_REFRESH_TOKEN\n{refresh}\n")
    print("=" * 45)
    print("In values ko kahin paste/share na karo. Sirf GitHub Secrets me daalo.")


if __name__ == "__main__":
    main()
