"""S3-compatible storage (Cloudflare R2 / AWS S3 / Backblaze B2 / MinIO).

Kyun zaroori hai:
 1. GitHub repo me bade video files rakhna theek nahi (100MB/file limit + bloat).
    Videos storage par rakho, repo me sirf queue.csv (text) rahe.
 2. Instagram Graph API local file accept nahi karta — usko public https URL
    chahiye. Normalize ke baad hum vertical version wapas upload karke uska
    public URL IG ko dete hain.

Secrets: S3_ENDPOINT, S3_BUCKET, S3_ACCESS_KEY, S3_SECRET_KEY, S3_PUBLIC_BASE
"""
import os

import requests


def _client():
    import boto3
    from botocore.config import Config

    endpoint = os.environ.get("S3_ENDPOINT")
    access = os.environ.get("S3_ACCESS_KEY")
    secret = os.environ.get("S3_SECRET_KEY")
    if not (endpoint and access and secret):
        return None
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access,
        aws_secret_access_key=secret,
        config=Config(signature_version="s3v4", retries={"max_attempts": 5}),
        region_name=os.environ.get("S3_REGION", "auto"),
    )


def available():
    return bool(
        os.environ.get("S3_ENDPOINT")
        and os.environ.get("S3_ACCESS_KEY")
        and os.environ.get("S3_SECRET_KEY")
        and os.environ.get("S3_BUCKET")
    )


def download(url, dest):
    """Kisi bhi public URL se video download karo (streaming, memory-safe)."""
    os.makedirs(os.path.dirname(os.path.abspath(dest)), exist_ok=True)
    with requests.get(url, stream=True, timeout=600) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    f.write(chunk)
    size_mb = os.path.getsize(dest) / 1024 / 1024
    print(f"[storage] downloaded {size_mb:.1f} MB -> {os.path.basename(dest)}")
    return dest


def download_key(key, dest):
    """Bucket ke andar se private key download karo."""
    c = _client()
    if c is None:
        raise RuntimeError("S3 secrets missing")
    os.makedirs(os.path.dirname(os.path.abspath(dest)), exist_ok=True)
    c.download_file(os.environ["S3_BUCKET"], key, dest)
    print(f"[storage] pulled s3://{os.environ['S3_BUCKET']}/{key}")
    return dest


def upload_public(local_path, key):
    """Normalized video upload karke uska public URL return karo (IG ke liye)."""
    c = _client()
    if c is None:
        raise RuntimeError("S3 secrets missing")
    bucket = os.environ["S3_BUCKET"]
    c.upload_file(
        local_path, bucket, key,
        ExtraArgs={"ContentType": "video/mp4", "CacheControl": "public, max-age=86400"},
    )

    base = os.environ.get("S3_PUBLIC_BASE", "").rstrip("/")
    if base:
        url = f"{base}/{key}"
    else:
        # public base na ho to 6 ghante ka signed URL (IG fetch kar leta hai)
        url = c.generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket, "Key": key},
            ExpiresIn=6 * 3600,
        )
    print(f"[storage] uploaded -> {key}")
    return url
