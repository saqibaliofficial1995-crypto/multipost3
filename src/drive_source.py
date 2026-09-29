"""Google Drive source for Multipost.

Handles:
- Listing video files (mp4, mov, m4v) in 'Multipost Inbox' folder.
- Skipping files already present in state/uploaded.json.
- Sorting oldest first.
- Downloading chosen video and optional same-named .txt file for metadata.
- Extracting metadata (title, description, tags).
- Moving processed files to the 'Uploaded' subfolder.
"""
import os
import re
import socket
import urllib3.util.connection

# Ensure IPv4 is preferred to prevent TLS hangs on networks with broken IPv6
try:
    urllib3.util.connection.allowed_gai_family = lambda: socket.AF_INET
except Exception:
    pass

import requests

DRIVE_API = "https://www.googleapis.com/drive/v3"
VIDEO_EXTS = (".mp4", ".mov", ".m4v")


def get_drive_session(token):
    session = requests.Session()
    session.headers.update({
        "Authorization": f"Bearer {token}",
        "Accept": "application/json"
    })
    return session


def get_or_create_uploaded_folder(session, inbox_id):
    """Finds or creates the 'Uploaded' subfolder inside the Inbox folder."""
    q = f"name = 'Uploaded' and '{inbox_id}' in parents and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
    resp = session.get(f"{DRIVE_API}/files", params={"q": q, "spaces": "drive", "fields": "files(id, name)"}, timeout=30)
    resp.raise_for_status()
    files = resp.json().get("files", [])
    if files:
        return files[0]["id"]

    meta = {
        "name": "Uploaded",
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [inbox_id]
    }
    create_resp = session.post(f"{DRIVE_API}/files", json=meta, params={"fields": "id, name"}, timeout=30)
    create_resp.raise_for_status()
    return create_resp.json()["id"]


def list_pending_videos(session, inbox_id, uploaded_ids_set):
    """Lists video files in the inbox folder, sorted oldest first, excluding uploaded ones."""
    q = f"'{inbox_id}' in parents and trashed = false and mimeType != 'application/vnd.google-apps.folder'"
    all_files = []
    page_token = None

    while True:
        params = {
            "q": q,
            "spaces": "drive",
            "fields": "nextPageToken, files(id, name, mimeType, createdTime, size)",
            "pageSize": 100,
            "orderBy": "createdTime asc"
        }
        if page_token:
            params["pageToken"] = page_token

        resp = session.get(f"{DRIVE_API}/files", params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        all_files.extend(data.get("files", []))

        page_token = data.get("nextPageToken")
        if not page_token:
            break

    candidates = []
    for f in all_files:
        name = f.get("name", "")
        file_id = f.get("id", "")
        ext = os.path.splitext(name)[1].lower()
        if ext in VIDEO_EXTS:
            if file_id not in uploaded_ids_set:
                candidates.append(f)

    # Sort oldest first by createdTime deterministically (using (createdTime, id) as tiebreaker)
    candidates.sort(key=lambda x: (x.get("createdTime", ""), x.get("id", "")))
    return candidates


def download_file(session, file_id, dest_path):
    """Streams a file download from Google Drive to dest_path."""
    os.makedirs(os.path.dirname(os.path.abspath(dest_path)), exist_ok=True)
    resp = session.get(f"{DRIVE_API}/files/{file_id}?alt=media", stream=True, timeout=120)
    resp.raise_for_status()
    with open(dest_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=1024 * 1024):
            if chunk:
                f.write(chunk)
    return dest_path


def get_sidecar_txt(session, inbox_id, video_name):
    """Checks if a same-named .txt file exists in the inbox and fetches its text."""
    base_name = os.path.splitext(video_name)[0]
    txt_name = f"{base_name}.txt"
    q = f"'{inbox_id}' in parents and name = '{txt_name}' and trashed = false"
    resp = session.get(f"{DRIVE_API}/files", params={"q": q, "spaces": "drive", "fields": "files(id, name)"}, timeout=30)
    resp.raise_for_status()
    files = resp.json().get("files", [])
    if not files:
        return None, None

    txt_file = files[0]
    content_resp = session.get(f"{DRIVE_API}/files/{txt_file['id']}?alt=media", timeout=30)
    content_resp.raise_for_status()
    text = content_resp.text.strip()
    return txt_file["id"], text


def extract_metadata(video_name, sidecar_text=None, default_desc="", default_tags=None):
    """Extracts title, description, and hashtags/tags from sidecar text or filename."""
    if default_tags is None:
        default_tags = []

    if sidecar_text:
        lines = [line.strip() for line in sidecar_text.splitlines() if line.strip()]
        title = lines[0] if lines else ""
        description = "\n".join(lines[1:]) if len(lines) > 1 else default_desc
    else:
        # Title comes from filename, extension stripped, underscores to spaces
        raw_name = os.path.splitext(video_name)[0]
        cleaned_name = raw_name.replace("_", " ").strip()
        title = cleaned_name
        description = default_desc

    # Extract hashtags (#hashtag) from title and description
    found_hashtags = re.findall(r"#([A-Za-z0-9_]+)", f"{title} {description}")
    tags = list(dict.fromkeys(default_tags + [h.lower() for h in found_hashtags]))

    return {
        "title": title,
        "description": description,
        "tags": tags
    }


def move_file_to_uploaded(session, file_id, inbox_id, uploaded_id):
    """Moves a file from inbox to the Uploaded subfolder."""
    url = f"{DRIVE_API}/files/{file_id}"
    params = {
        "addParents": uploaded_id,
        "removeParents": inbox_id,
        "fields": "id, parents"
    }
    resp = session.patch(url, params=params, json={}, timeout=30)
    resp.raise_for_status()
    return resp.json()
