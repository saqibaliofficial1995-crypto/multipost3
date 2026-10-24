"""AI-powered YouTube & Reels metadata generator using Google Gemini.

Features:
1. Video Multimodal Analysis:
   - Uploads normalized MP4 to Gemini Files API (resumable protocol).
   - Waits for state to become ACTIVE.
   - Prompts Gemini to analyze visual scenes, luxury elements, and narrative.
   - Deletes uploaded file from Gemini Files API in a finally block.
2. Model Discovery:
   - Dynamically checks available models for the user's API key.
   - Chooses the best free-tier model supporting video (e.g. gemini-2.0-flash or gemini-1.5-flash).
3. Fallback Hierarchy:
   - Video analysis (video_ai_gemini)
   - Text-only Gemini call (text_ai_gemini)
   - Topic & config default metadata (topic_default_fallback)
   - Publishing never breaks if AI fails.
4. Output Schema & Strict YouTube Constraints:
   - title: under 60 characters, main keyword near front, no clickbait/ALL CAPS.
   - description: 3 to 5 lines. Hook in first 2 lines. Natural sentences.
   - tags: 10 to 15 items, total under 460 characters. Includes 'shorts' if duration <= 60s.
   - hashtags: exactly 3, appended to description.
"""
import json
import os
import re
import socket
import time
import urllib3.util.connection

try:
    urllib3.util.connection.allowed_gai_family = lambda: socket.AF_INET
except Exception:
    pass

import requests

BASE_API = "https://generativelanguage.googleapis.com/v1beta"
UPLOAD_API = "https://generativelanguage.googleapis.com/upload/v1beta/files"


def clean_text(text):
    """Removes excessive emoji-spam, shouty ALL CAPS, and any markdown code-fence remnants."""
    if not text:
        return ""
    text = str(text).strip()
    # Strip markdown fences and language specifiers at start and end
    text = re.sub(r"^```(?:text|markdown|json|plain|txt)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)
    text = re.sub(r"^(?:text|description|title)\s*:\s*", "", text, flags=re.IGNORECASE)
    # Strip excessive consecutive emojis (limit to max 1)
    text = re.sub(r"([\U00010000-\U0010ffff\u2600-\u27bf]){2,}", r"\1", text)
    # If text is predominantly ALL CAPS (and > 10 chars), convert to capitalized sentence
    letters = [c for c in text if c.isalpha()]
    if letters and (sum(1 for c in letters if c.isupper()) / len(letters)) > 0.6:
        text = text.capitalize()
    return text.strip()


def validate_metadata(data, duration=0, channel_topic="", is_text_only=False):
    """Validates the AI output strictly against YouTube requirements and anti-hallucination rules."""
    if isinstance(data, list) and len(data) > 0 and isinstance(data[0], dict):
        data = data[0]

    if not isinstance(data, dict):
        print(f"[llm_meta] Validation reject: Root payload is not a dict: {type(data)}")
        return None

    title = clean_text(str(data.get("title", "")))
    raw_desc = str(data.get("description", "")).strip()
    tags = data.get("tags", [])
    hashtags = data.get("hashtags", [])

    # Title must be under 85 characters and non-empty (leaves room for hook + emoji + #shorts)
    if not title or len(title) > 85:
        print(f"[llm_meta] Validation reject: Title length invalid ({len(title)} chars): '{title}'")
        return None

    # For shorts, ensure #shorts is appended to title if space allows
    if duration <= 180 and "#shorts" not in title.lower():
        if len(title) + 8 <= 85:
            title = f"{title} #shorts"

    # ANTI-HALLUCINATION RULE 1: Reject any title containing currency symbols or codes
    # ($, €, £, ¥, ₹, USD, AED, EUR, GBP, etc.)
    if re.search(r"[\$\€\£\¥\₹]|\b(USD|AED|EUR|GBP)\b", title, re.IGNORECASE):
        print(f"[llm_meta] Validation reject: Title contains currency symbol/code: '{title}'")
        return None

    # ANTI-HALLUCINATION RULE 2: Reject any title containing numbers that imply a price or factual claim
    # e.g., 50M, 10M, 100k, 10,000 sqft, 5000 sq ft, 20M, etc.
    if re.search(r"\b\d+([.,]\d+)?\s*(m|b|k|million|billion|sqft|sq\s*ft|feet)\b", title, re.IGNORECASE):
        print(f"[llm_meta] Validation reject: Title contains price/metric claim: '{title}'")
        return None

    # ANTI-HALLUCINATION RULE 3: Reject large numbers (e.g. 50000, 100000) unless it's a current year
    large_nums = re.findall(r"\b\d{4,}\b", title)
    if large_nums and any(y not in ("2024", "2025", "2026", "2027") for y in large_nums):
        print(f"[llm_meta] Validation reject: Title contains large factual number: '{title}'")
        return None

    # ANTI-HALLUCINATION RULE 4: Reject price claims in description
    if re.search(r"[\$\€\£\¥\₹]|\b(USD|AED|EUR|GBP)\b|\b\d+([.,]\d+)?\s*(m|b|million|billion)\b", raw_desc, re.IGNORECASE):
        print(f"[llm_meta] Validation reject: Description contains price/valuation claims")
        return None

    # RESTRICTED TEXT MODE: When the AI model has NOT seen the video
    if is_text_only:
        # Must not invent any specific numbers implying factual claims
        if re.search(r"\b\d+\b", title):
            print(f"[llm_meta] Validation reject (text_ai restricted): Title contains numbers implying factual claims: '{title}'")
            return None
        # Must not use 'tour', 'inside', 'walkthrough', or 'look inside' framing
        if re.search(r"\b(tour|inside|walkthrough|look inside|step inside|peek inside)\b", title, re.IGNORECASE):
            print(f"[llm_meta] Validation reject (text_ai restricted): Title contains forbidden framing: '{title}'")
            return None

    # Description lines processing: 2 to 5 lines with hook in first 2 lines
    desc_lines = [clean_text(l) for l in raw_desc.splitlines() if l.strip()]
    desc_lines = [l for l in desc_lines if l and l.lower() != "text" and not l.lower().startswith("text:")]
    if len(desc_lines) == 1:
        # Split sentences to produce multi-line hook & supporting lines
        sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", desc_lines[0]) if s.strip()]
        if len(sentences) >= 2:
            desc_lines = sentences
        elif len(sentences) == 1:
            desc_lines = [sentences[0], f"Satisfying {channel_topic or 'dog mukbang ASMR'} crunches!"]
    elif len(desc_lines) == 0:
        desc_lines = [f"Enjoy relaxing {channel_topic or 'dog ASMR mukbang'} sounds.", f"Subscribe for daily satisfying pet videos!"]

    if len(desc_lines) > 5:
        desc_lines = desc_lines[:5]
    desc = "\n".join(desc_lines)

    # Normalize tags
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",") if t.strip()]
    elif not isinstance(tags, list):
        tags = []

    clean_tags = []
    seen = set()
    for t in tags:
        clean_t = re.sub(r"[#,\n\r]", "", str(t)).strip()
        if clean_t and clean_t.lower() not in seen:
            seen.add(clean_t.lower())
            clean_tags.append(clean_t)

    # For videos <= 60s, keep 'shorts' in tags
    if duration <= 60 and "shorts" not in seen:
        clean_tags.insert(0, "shorts")
        seen.add("shorts")

    # Ensure 10 to 15 tags by supplementing from niche pool
    if len(clean_tags) < 10:
        base_pool = [
            "dogasmr", "dogmukbang", "dogeating", "asmrsounds", "satisfying",
            "labrador", "cutedog", "asmrfood", "crunchysounds", "doglover", "pets", "viral", "shorts"
        ]
        if channel_topic:
            for w in channel_topic.split():
                if w.lower() not in seen and len(w) > 2:
                    clean_tags.append(w.capitalize())
                    seen.add(w.lower())
        for bp in base_pool:
            if len(clean_tags) >= 12:
                break
            if bp.lower() not in seen:
                clean_tags.append(bp)
                seen.add(bp.lower())

    clean_tags = clean_tags[:15]

    # Enforce total tags string length under 460 characters
    while clean_tags and len(",".join(clean_tags)) >= 460:
        clean_tags.pop()

    if len(clean_tags) < 5:
        print(f"[llm_meta] Validation reject: Insufficient tags ({len(clean_tags)})")
        return None

    # Exactly 3 clean hashtags
    if isinstance(hashtags, str):
        hashtags = [h.strip() for h in re.findall(r"#?\w+", hashtags) if h.strip()]
    elif not isinstance(hashtags, list):
        hashtags = []

    clean_hashtags = []
    for h in hashtags:
        tag = re.sub(r"[^A-Za-z0-9_]", "", str(h)).strip()
        if tag and f"#{tag}" not in clean_hashtags:
            clean_hashtags.append(f"#{tag}")

    backup_pool = [re.sub(r"[^A-Za-z0-9_]", "", t) for t in clean_tags if t.lower() != "shorts"]
    if channel_topic:
        backup_pool.insert(0, re.sub(r"[^A-Za-z0-9_]", "", channel_topic))
    for candidate in backup_pool:
        if len(clean_hashtags) >= 3:
            break
        if candidate and f"#{candidate}" not in clean_hashtags:
            clean_hashtags.append(f"#{candidate}")

    clean_hashtags = clean_hashtags[:3]

    # Append the 3 hashtags to the end of the description
    if clean_hashtags:
        ht_line = " ".join(clean_hashtags)
        if ht_line not in desc:
            desc = f"{desc}\n\n{ht_line}"

    return {
        "title": title,
        "description": desc,
        "tags": clean_tags,
        "hashtags": clean_hashtags
    }


def discover_models(api_key):
    """Discovers available models for the given API key and returns prioritized list."""
    preferred = [
        "gemini-flash-lite-latest",
        "gemini-flash-latest",
        "gemini-3.1-flash-lite",
        "gemini-3-flash-preview",
        "gemini-3.1-flash-lite-preview",
        "gemini-3.5-flash",
        "gemini-3.6-flash",
        "gemini-2.0-flash",
        "gemini-1.5-flash",
    ]
    headers = {"x-goog-api-key": api_key, "Accept": "application/json"}
    try:
        r = requests.get(f"{BASE_API}/models", headers=headers, params={"key": api_key}, timeout=15)
        if r.status_code == 200:
            models_data = r.json().get("models", [])
            supported = [
                m.get("name", "").replace("models/", "")
                for m in models_data
                if "generateContent" in m.get("supportedGenerationMethods", [])
                and "2.5-flash" not in m.get("name", "")
                and "2.5-pro" not in m.get("name", "")
            ]
            print(f"[llm_meta] Supported active models: {supported}")
            ordered = [p for p in preferred if p in supported]
            for s in supported:
                if s not in ordered and "flash" in s:
                    ordered.append(s)
            for s in supported:
                if s not in ordered:
                    ordered.append(s)
            if ordered:
                return ordered
        else:
            print(f"[llm_meta] Models list failed (HTTP {r.status_code}): {r.text}")
    except Exception as e:
        print(f"[llm_meta] Model discovery warning: {e}")
    return ["gemini-flash-lite-latest", "gemini-flash-latest", "gemini-3.5-flash", "gemini-3.6-flash"]


def discover_model(api_key):
    """Returns the primary discovered model."""
    return discover_models(api_key)[0]


def upload_video_to_gemini(video_path, api_key):
    """Uploads a video to Gemini Files API using resumable upload and waits until ACTIVE."""
    file_size = os.path.getsize(video_path)
    display_name = os.path.basename(video_path)
    mime_type = "video/mp4"

    # 1. Initiate resumable upload
    init_headers = {
        "x-goog-api-key": api_key,
        "X-Goog-Upload-Protocol": "resumable",
        "X-Goog-Upload-Command": "start",
        "X-Goog-Upload-Header-Content-Length": str(file_size),
        "X-Goog-Upload-Header-Content-Type": mime_type,
        "Content-Type": "application/json"
    }
    init_body = json.dumps({"file": {"display_name": display_name}})
    init_resp = requests.post(
        UPLOAD_API,
        headers=init_headers,
        params={"key": api_key},
        data=init_body,
        timeout=30
    )
    if init_resp.status_code != 200:
        print(f"[llm_meta] Files API init failed (HTTP {init_resp.status_code}): {init_resp.text}")
        init_resp.raise_for_status()

    upload_url = init_resp.headers.get("X-Goog-Upload-URL") or init_resp.headers.get("x-goog-upload-url")
    if not upload_url:
        raise RuntimeError("Missing X-Goog-Upload-URL in Gemini Files API response")

    # 2. Upload file content
    with open(video_path, "rb") as f:
        file_bytes = f.read()

    upload_headers = {
        "x-goog-api-key": api_key,
        "Content-Length": str(file_size),
        "X-Goog-Upload-Offset": "0",
        "X-Goog-Upload-Command": "upload, finalize"
    }
    upload_resp = requests.post(upload_url, headers=upload_headers, data=file_bytes, timeout=180)
    if upload_resp.status_code != 200:
        print(f"[llm_meta] Files API upload chunk failed (HTTP {upload_resp.status_code}): {upload_resp.text}")
        upload_resp.raise_for_status()

    file_info = upload_resp.json().get("file", {})
    remote_name = file_info.get("name")
    remote_uri = file_info.get("uri")

    # 3. Wait for processing to become ACTIVE
    start_time = time.time()
    while time.time() - start_time < 120:
        poll_resp = requests.get(
            f"{BASE_API}/{remote_name}",
            headers={"x-goog-api-key": api_key},
            params={"key": api_key},
            timeout=20
        )
        if poll_resp.status_code != 200:
            print(f"[llm_meta] Files API poll error (HTTP {poll_resp.status_code}): {poll_resp.text}")
        poll_resp.raise_for_status()
        state = poll_resp.json().get("state")
        if state == "ACTIVE":
            return remote_name, remote_uri
        elif state == "FAILED":
            raise RuntimeError(f"Gemini file state FAILED for {remote_name}")
        time.sleep(3)

    raise TimeoutError(f"Gemini file processing timed out for {remote_name}")


def delete_gemini_file(remote_name, api_key):
    """Deletes an uploaded file from Gemini Files API."""
    if not remote_name:
        return
    try:
        r = requests.delete(
            f"{BASE_API}/{remote_name}",
            headers={"x-goog-api-key": api_key},
            params={"key": api_key},
            timeout=15
        )
        if r.status_code in (200, 204):
            print(f"[llm_meta] Confirmed: Uploaded file {remote_name} deleted from Gemini Files API.")
        else:
            print(f"[llm_meta] Warning deleting {remote_name} (HTTP {r.status_code}): {r.text}")
    except Exception as e:
        print(f"[llm_meta] Error deleting {remote_name}: {e}")


def generate_from_video(video_path, duration, channel_topic, api_key, candidate_models):
    """Analyzes the video using Gemini multimodal input with exponential backoff and model switching."""
    if isinstance(candidate_models, str):
        candidate_models = [candidate_models]
    if not candidate_models:
        candidate_models = ["gemini-flash-latest", "gemini-3.5-flash", "gemini-3.6-flash"]

    remote_name = None
    try:
        print(f"[llm_meta] Uploading video to Gemini Files API ({os.path.basename(video_path)})...")
        remote_name, remote_uri = upload_video_to_gemini(video_path, api_key)
        print(f"[llm_meta] Video uploaded and ACTIVE ({remote_name}). Analyzing content...")

        prompt = f"""You are an elite YouTube Shorts growth & viral SEO specialist.
Analyze this video carefully.
Channel topic / niche: '{channel_topic}'.
Video duration: {duration} seconds.

VIRAL SHORTS CRITICAL RULES:
- Focus on what the dog/pet is eating, the ASMR sounds, crunchy textures, and cute/satisfying reactions.
- Write a high-CTR, curiosity-driven title mentioning food items (e.g. steak, egg, fish, salmon, crunch, sound ON, satisfying).
- Use 1-2 relevant emojis (🥩, 🍖, 🐶, 🐾, 🤤, 🎧) to grab visual attention on mobile Shorts feeds.
- Title MUST be under 75 characters so '#shorts' can easily fit.
- NO ALL CAPS yelling, NO fake claims or prices.
- Ground the description in the visible food and relaxing ASMR eating experience.

Output MUST be raw valid JSON adhering strictly to this schema:
{{
  "title": "Exciting, high-CTR hook title UNDER 75 characters (e.g., 'Dog ASMR: Crunchy Steak & Egg Feast! 🥩🐶')",
  "description": "3 to 4 lines total. First line is an engaging hook about the delicious meal/crunch. Followed by a warm call to subscribe for daily satisfying dog ASMR.",
  "tags": ["10 to 15 targeted keywords like dogasmr, dogmukbang, labrador, asmr, satisfying, dogeating, pets"],
  "hashtags": ["shorts", "dogasmr", "mukbang"]
}}
"""

        payload = {
            "contents": [
                {
                    "parts": [
                        {"fileData": {"mimeType": "video/mp4", "fileUri": remote_uri}},
                        {"text": prompt}
                    ]
                }
            ],
            "generationConfig": {
                "temperature": 0.2,
                "responseMimeType": "application/json"
            }
        }

        max_attempts = 6
        backoff_delays = [2, 4, 6, 8, 12, 16]
        model_idx = 0
        consecutive_503 = 0

        for attempt in range(1, max_attempts + 1):
            current_model = candidate_models[model_idx % len(candidate_models)]
            gen_url = f"{BASE_API}/models/{current_model}:generateContent"
            print(f"[llm_meta] Video analysis attempt {attempt}/{max_attempts} using model '{current_model}'...")

            status_code = None
            resp_text = ""
            try:
                resp = requests.post(
                    gen_url,
                    headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
                    params={"key": api_key},
                    json=payload,
                    timeout=90
                )
                status_code = resp.status_code
                resp_text = resp.text
            except Exception as req_err:
                print(f"[llm_meta] Attempt {attempt}/{max_attempts} network exception: {req_err}")
                status_code = 0
                resp_text = str(req_err)

            print(f"[llm_meta] Attempt {attempt}/{max_attempts} status: HTTP {status_code}")

            if status_code == 200:
                try:
                    cand = resp.json().get("candidates", [{}])[0]
                    text_content = cand.get("content", {}).get("parts", [{}])[0].get("text", "")
                    parsed = json.loads(text_content)
                    validated = validate_metadata(parsed, duration=duration, channel_topic=channel_topic, is_text_only=False)
                    if validated:
                        validated["path_used"] = "video_ai_gemini"
                        validated["model_used"] = current_model
                        print(f"[llm_meta] Video analysis succeeded on attempt {attempt} using '{current_model}'.")
                        return validated
                    else:
                        print(f"[llm_meta] Attempt {attempt} output failed validation: {text_content[:200]}")
                except Exception as parse_err:
                    print(f"[llm_meta] Attempt {attempt} response parse error: {parse_err}")

                if len(candidate_models) > 1:
                    model_idx += 1
            elif status_code == 503:
                consecutive_503 += 1
                print(f"[llm_meta] Attempt {attempt}/{max_attempts} returned 503 Service Unavailable ({consecutive_503} in a row): {resp_text[:250]}")
                if consecutive_503 >= 1 and len(candidate_models) > 1:
                    model_idx += 1
                    next_model = candidate_models[model_idx % len(candidate_models)]
                    print(f"[llm_meta] Switching to next available model from live list: '{next_model}'")
            elif status_code in (429, 500, 502, 504):
                print(f"[llm_meta] Attempt {attempt}/{max_attempts} transient status {status_code}: {resp_text[:200]}")
                if len(candidate_models) > 1:
                    model_idx += 1
            else:
                print(f"[llm_meta] Attempt {attempt}/{max_attempts} error (HTTP {status_code}): {resp_text[:200]}")
                if len(candidate_models) > 1:
                    model_idx += 1

            if attempt < max_attempts:
                sleep_sec = backoff_delays[attempt - 1]
                next_model = candidate_models[model_idx % len(candidate_models)]
                print(f"[llm_meta] Waiting {sleep_sec}s exponential backoff before retry {attempt + 1} (model: '{next_model}')...")
                time.sleep(sleep_sec)

    finally:
        if remote_name:
            print(f"[llm_meta] Cleaning up Gemini file {remote_name}...")
            delete_gemini_file(remote_name, api_key)

    return None


def generate_from_text(duration, channel_topic, api_key, model_name, filename=""):
    """Text-only Gemini fallback (RESTRICTED MODE: model has NOT seen video)."""
    prompt = f"""You are an elite YouTube SEO metadata specialist.
Generate metadata for a video in this channel topic / niche: '{channel_topic}'.
Video duration: {duration} seconds.

CRITICAL RESTRICTION (YOU HAVE NOT SEEN THE VIDEO):
- You MUST NOT invent any specific factual claims, events, objects, or figures.
- NO prices, valuations, or monetary figures (absolutely NO $, USD, AED, M, K, etc.).
- NO square footage, dimensions, or acreage.
- NO brand names or private property names.
- NO locations beyond the general niche '{channel_topic}'.
- NO 'tour', 'inside', 'walkthrough', or 'look inside' framing.
- Focus purely on broad atmospheric lifestyle, design inspiration, and aesthetic concepts.

Output MUST be raw valid JSON adhering strictly to this schema:
{{
  "title": "Aesthetic, atmospheric title UNDER 60 characters for this niche. NO factual claims, NO prices, NO currency symbols, NO tour/inside framing, and NO ALL CAPS.",
  "description": "3 to 5 lines total. Compelling hook in the first 2 lines focused on design/lifestyle inspiration. Followed by natural sentences with relevant niche keywords.",
  "tags": ["10 to 15 tag keywords mixing broad and specific, total under 450 characters"],
  "hashtags": ["Three relevant hashtags without '#', exactly 3 items"]
}}
"""
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.3,
            "responseMimeType": "application/json"
        }
    }
    try:
        gen_url = f"{BASE_API}/models/{model_name}:generateContent"
        resp = requests.post(
            gen_url,
            headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
            params={"key": api_key},
            json=payload,
            timeout=30
        )
        if resp.status_code != 200:
            print(f"[llm_meta] Text generateContent error (HTTP {resp.status_code}): {resp.text}")
            resp.raise_for_status()

        cand = resp.json().get("candidates", [{}])[0]
        text_content = cand.get("content", {}).get("parts", [{}])[0].get("text", "")

        parsed = json.loads(text_content)
        validated = validate_metadata(parsed, duration=duration, channel_topic=channel_topic, is_text_only=True)
        if validated:
            validated["path_used"] = "text_ai_gemini"
            validated["model_used"] = model_name
            return validated
    except Exception as e:
        print(f"[llm_meta] Text-only Gemini call error: {e}")

    return None


def fallback_metadata(duration=0, channel_topic="", default_desc="", default_tags=None):
    """Safe fallback based on channel topic and configuration defaults.
    
    Guarantees:
    - title under 75 chars with #shorts
    - description with hook and 3 hashtags
    - exactly 10 to 15 tags, total under 460 chars
    """
    topic = channel_topic or "Dog ASMR Mukbang Eating"
    title = f"{topic} #shorts"[:75]

    # Curate high-relevance tags for channel niche
    base_tags = [
        "dogasmr", "dogmukbang", "dogeating", "asmrsounds", "satisfying",
        "labrador", "cutedog", "asmrfood", "crunchysounds", "doglover", "pets", "shorts"
    ]
    tags = list(default_tags) if default_tags else []
    for t in base_tags:
        if t not in tags:
            tags.append(t)
    if duration <= 180 and "shorts" not in [t.lower() for t in tags]:
        tags.insert(0, "shorts")

    # Pad to at least 10 tags if needed
    padding = ["trending", "viral", "asmr", "satisfyingmukbang", "doglove"]
    for p in padding:
        if len(tags) >= 10:
            break
        if p not in tags:
            tags.append(p)

    # Slice to at most 15 tags
    tags = tags[:15]

    # Enforce under 460 characters total
    while tags and len(",".join(tags)) >= 460:
        tags.pop()

    hashtags = ["#shorts", "#dogasmr", "#mukbang"]
    desc = default_desc or f"Satisfying {topic.lower()} sounds!\nWatch our happy dog enjoy delicious meals.\nSubscribe for daily relaxing pet ASMR."
    desc = f"{desc}\n\n{' '.join(hashtags)}"

    return {
        "title": title,
        "description": desc,
        "tags": tags,
        "hashtags": hashtags,
        "path_used": "topic_default_fallback"
    }


def generate_metadata(video_path=None, filename="", duration=0, channel_topic="", api_key=None, default_desc="", default_tags=None):
    """Main entry point: Generates metadata with multi-tier fallback.

    Tier 1: Multimodal video analysis via Gemini Files API with exponential backoff & model switching (video_ai_gemini).
    Tier 2: Text-only Gemini call with strict anti-hallucination restrictions (text_ai_gemini).
    Tier 3: Channel topic and config defaults (topic_default_fallback).
    """
    key = api_key or os.environ.get("GEMINI_API_KEY")

    if key:
        candidate_models = discover_models(key)
        chosen_model = candidate_models[0]
        print(f"[llm_meta] Candidate models from live list: {candidate_models} (primary: {chosen_model})")

        # Tier 1: Multimodal video analysis
        if video_path and os.path.exists(video_path):
            try:
                res = generate_from_video(video_path, duration, channel_topic, key, candidate_models)
                if res:
                    print(f"[llm_meta] Generated metadata via Tier 1 ({res['path_used']}) using '{res.get('model_used', chosen_model)}'.")
                    return res
            except Exception as e:
                print(f"[llm_meta] Tier 1 video analysis failed ({e}). Falling back to Tier 2.")

        # Tier 2: Text-only Gemini call
        try:
            res = generate_from_text(duration, channel_topic, key, chosen_model, filename)
            if res:
                print(f"[llm_meta] Generated metadata via Tier 2 ({res['path_used']}).")
                return res
        except Exception as e:
            print(f"[llm_meta] Tier 2 text generation failed ({e}). Falling back to Tier 3.")
    else:
        print("[llm_meta] GEMINI_API_KEY is not set. Using Tier 3 fallback.")

    # Tier 3: Config and channel topic defaults
    res = fallback_metadata(duration, channel_topic, default_desc, default_tags)
    print(f"[llm_meta] Generated metadata via Tier 3 ({res['path_used']}).")
    return res


def infer_channel_topic(sample_items, api_key=None):
    """Samples up to 3 videos to infer the channel topic / niche using Gemini."""
    key = api_key or os.environ.get("GEMINI_API_KEY")
    if not sample_items:
        return "General creator lifestyle"

    items_to_sample = sample_items[:3]
    titles = [os.path.splitext(os.path.basename(str(s)))[0] for s in items_to_sample]

    if not key:
        print("[llm_meta] GEMINI_API_KEY not set for topic inference. Using sample title theme.")
        return titles[0].replace("_", " ").replace("-", " ")[:35]

    try:
        candidate_models = discover_models(key)
        chosen_model = candidate_models[0]
        prompt = (
            "You are an AI assisting a YouTube channel automation system.\n"
            "Analyze these sample video titles/files (up to 3) from the channel inbox:\n"
            + "\n".join(f"- {t}" for t in titles) + "\n\n"
            "Infer the overarching channel niche or topic in 2 to 5 words (e.g. 'Dubai luxury lifestyle', 'Minecraft gaming guides', 'Personal finance tips').\n"
            "Return ONLY a JSON object in this format:\n"
            '{"channel_topic": "..."}\n'
            "Do not include markdown fences or any explanation."
        )
        gen_url = f"{BASE_API}/models/{chosen_model}:generateContent"
        r = requests.post(
            gen_url,
            headers={"Content-Type": "application/json"},
            params={"key": key},
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"temperature": 0.2, "responseMimeType": "application/json"}
            },
            timeout=20
        )
        if r.status_code == 200:
            data = r.json()
            raw_text = data.get("candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text", "")
            parsed = json.loads(raw_text)
            topic = parsed.get("channel_topic", "").strip()
            if topic:
                print(f"[llm_meta] Inferred channel topic from {len(items_to_sample)} sample(s): '{topic}'")
                return topic
    except Exception as e:
        print(f"[llm_meta] Topic inference call warning ({e}).")

    fallback_topic = titles[0].replace("_", " ").replace("-", " ")[:35]
    return fallback_topic


def infer_topic_from_video(video_path, api_key=None):
    """Watches the actual video via Gemini multimodal Files API to infer the channel topic/niche.
    NEVER uses filenames, folder names, or text heuristics.
    """
    key = api_key or os.environ.get("GEMINI_API_KEY")
    if not key:
        raise RuntimeError("GEMINI_API_KEY required for multimodal topic detection.")

    if not os.path.exists(video_path):
        raise FileNotFoundError(f"Video file not found for topic inference: {video_path}")

    candidate_models = discover_models(key)
    chosen_model = candidate_models[0]

    remote_name = None
    try:
        print(f"[llm_meta] Uploading video to Gemini Files API to watch visual content for topic detection...")
        remote_name, remote_uri = upload_video_to_gemini(video_path, key)

        prompt = (
            "You are an expert YouTube content analyst.\n"
            "WATCH THIS VIDEO CAREFULLY. Observe the visual scenes, actions, subject (dog eating food), and ambiance.\n"
            "Identify the exact niche or overarching topic of this channel/content in 2 to 4 words "
            "(for example: 'Dog ASMR Mukbang', 'Pet Food ASMR', 'Dog Eating Treats', 'Satisfying ASMR Mukbang').\n"
            "DO NOT use filenames, numbers, or generic words. Focus strictly on what is visually shown.\n"
            "Return ONLY a JSON object:\n"
            '{"channel_topic": "..."}\n'
            "No markdown fences, no extra text."
        )

        gen_url = f"{BASE_API}/models/{chosen_model}:generateContent"
        payload = {
            "contents": [{
                "parts": [
                    {"fileData": {"mimeType": "video/mp4", "fileUri": remote_uri}},
                    {"text": prompt}
                ]
            }],
            "generationConfig": {
                "temperature": 0.1,
                "responseMimeType": "application/json"
            }
        }

        r = requests.post(
            gen_url,
            headers={"Content-Type": "application/json"},
            params={"key": key},
            json=payload,
            timeout=60
        )
        if r.status_code == 200:
            data = r.json()
            raw_text = data.get("candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text", "")
            parsed = json.loads(raw_text)
            topic = parsed.get("channel_topic", "").strip()
            if topic:
                print(f"[llm_meta] Gemini watched the video and inferred channel topic: '{topic}'")
                return topic
        else:
            print(f"[llm_meta] Video topic detection error (HTTP {r.status_code}): {r.text}")
    finally:
        if remote_name:
            print(f"[llm_meta] Cleaning up Gemini topic analysis file: {remote_name}")
            delete_gemini_file(remote_name, key)

    print("[meta] topic fallback used", flush=True)
    return "Dog ASMR Mukbang"


def infer_channel_topic(sample_items, api_key=None, video_path=None):
    """Backwards-compatible wrapper that watches video if available."""
    if video_path and os.path.exists(video_path):
        return infer_topic_from_video(video_path, api_key)
    print("[meta] topic fallback used", flush=True)
    return "Dog ASMR Mukbang"
