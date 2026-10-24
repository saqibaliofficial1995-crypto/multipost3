# Multipost Channel 3 (Marie Reed)

Standalone automated pipeline for scheduled YouTube Shorts publishing from Google Drive inbox for Channel 3 (**Marie Reed**).

## Channel Isolation
- **Owning Google Account:** `galopalam@gmail.com`
- **YouTube Channel ID:** `UC9MM67B95X6B0fjhMzcixBw`
- **GCP Project:** `yt-multipost-channel3`
- **GitHub Account:** `saqibaliofficial1995-crypto`
- **Domain:** `https://saqibaliofficial1995-crypto.github.io/`

## Daily Operation
- Videos staged in Google Drive Inbox.
- GitHub Actions triggers 5x daily.
- Multimodal Gemini infer_topic_from_video inspects actual video frames.
- Anti-hallucination validation and publish safety gate ensure high quality.
- Videos published publicly, state committed, and file moved to Uploaded subfolder.
