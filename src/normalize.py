"""ffmpeg se video ko Shorts/Reels-safe banata hai.

Kyun zaroori hai: agar video 9:16 vertical aur <=3 min nahi hai to YouTube use
Shorts feed me nahi daalta aur Instagram Reels reject/crop kar deta hai.
Yahi "views nahi aate" ki sabse badi technical wajah hai.
"""
import json
import os
import subprocess


def probe(path):
    out = subprocess.run(
        [
            "ffprobe", "-v", "quiet", "-print_format", "json",
            "-show_format", "-show_streams", path,
        ],
        capture_output=True, text=True, check=True,
    ).stdout
    data = json.loads(out)
    v = next((s for s in data["streams"] if s["codec_type"] == "video"), None)
    a = next((s for s in data["streams"] if s["codec_type"] == "audio"), None)
    return {
        "duration": float(data["format"].get("duration", 0) or 0),
        "width": int(v["width"]) if v else 0,
        "height": int(v["height"]) if v else 0,
        "has_audio": a is not None,
    }


def normalize(src, out_path, enc):
    """Vertical 1080x1920 me convert karo: blurred background + centered video.
    Agar already vertical hai to sirf scale + pad hoga (koi quality loss nahi).
    """
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    w, h = enc["width"], enc["height"]
    info = probe(src)

    # blurred-fill background so horizontal videos bhi vertical feed me bhare
    vf = (
        f"split=2[bg][fg];"
        f"[bg]scale={w}:{h}:force_original_aspect_ratio=increase,"
        f"crop={w}:{h},gblur=sigma=28[bgb];"
        f"[fg]scale={w}:{h}:force_original_aspect_ratio=decrease[fgs];"
        f"[bgb][fgs]overlay=(W-w)/2:(H-h)/2,"
        f"fps={enc['fps']},format=yuv420p"
    )

    cmd = [
        "ffmpeg", "-y", "-i", src,
        "-t", str(enc["max_seconds"]),
        "-filter_complex", vf,
        "-c:v", "libx264", "-preset", "medium", "-profile:v", "high",
        "-b:v", enc["video_bitrate"], "-maxrate", enc["video_bitrate"],
        "-bufsize", "12M", "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
    ]

    if info["has_audio"]:
        cmd += ["-c:a", "aac", "-b:a", enc["audio_bitrate"], "-ar", "44100", "-ac", "2"]
    else:
        # IG/FB silent video reject kar dete hain -> silent track add karo
        cmd = cmd[:3] + [
            "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
        ] + cmd[3:] + ["-c:a", "aac", "-b:a", enc["audio_bitrate"], "-shortest"]

    cmd.append(out_path)
    subprocess.run(cmd, check=True, capture_output=True, text=True)
    return out_path
