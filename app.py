"""
NH YT Downloader — Railway Python Backend
yt-dlp powered with auto-update on startup.

Endpoints:
  GET /api/info?url=<youtube_url>
  GET /api/download?url=<youtube_url>&format_id=<id>
"""

import os
import subprocess
import json
import urllib.request
from flask import Flask, request, jsonify, Response, stream_with_context

app = Flask(__name__)

# ── Auto-update yt-dlp on startup ─────────────────────────────────────────────
try:
    subprocess.run(
        ["pip", "install", "-q", "--upgrade", "yt-dlp"],
        capture_output=True, timeout=120
    )
    print("yt-dlp updated successfully")
except Exception as e:
    print(f"yt-dlp update failed: {e}")

# ── Base yt-dlp args (bypass YouTube bot detection) ───────────────────────────
YT_BASE_ARGS = [
    "yt-dlp",
    "--no-playlist",
    "--extractor-retries", "3",
    "--socket-timeout", "30",
]

# ── helpers ───────────────────────────────────────────────────────────────────

def run_ytdlp_json(url: str) -> dict | None:
    try:
        result = subprocess.run(
            YT_BASE_ARGS + ["--dump-json", url],
            capture_output=True, text=True, timeout=90
        )
        if result.returncode != 0:
            print("yt-dlp error:", result.stderr[-500:])
            return None
        return json.loads(result.stdout)
    except Exception as e:
        print("run_ytdlp_json exception:", e)
        return None


def build_label(fmt: dict) -> str:
    vcodec = fmt.get("vcodec", "none")
    acodec = fmt.get("acodec", "none")
    height = fmt.get("height")
    ext    = fmt.get("ext", "")

    if vcodec != "none" and height:
        return f"Video {height}p ({ext.upper()})"
    elif vcodec != "none":
        return f"Video ({ext.upper()})"
    elif acodec != "none":
        abr = fmt.get("abr")
        if abr:
            return f"Audio Only {int(abr)}kbps ({ext.upper()})"
        return f"Audio Only ({ext.upper()})"
    else:
        return f"Format {fmt.get('format_id', '?')} ({ext.upper()})"


PREFERRED_HEIGHTS = [1080, 720, 480, 360, 240]

def select_best_formats(formats: list) -> list:
    video_audio = [f for f in formats if f.get("vcodec","none") != "none" and f.get("acodec","none") != "none"]
    video_only  = [f for f in formats if f.get("vcodec","none") != "none" and f.get("acodec","none") == "none"]
    audio_only  = [f for f in formats if f.get("vcodec","none") == "none"  and f.get("acodec","none") != "none"]

    selected = []
    seen_heights = set()

    for h in PREFERRED_HEIGHTS:
        candidates = [f for f in video_audio if f.get("height") == h and f.get("ext") == "mp4"]
        if not candidates:
            candidates = [f for f in video_audio if f.get("height") == h]
        if not candidates:
            candidates = [f for f in video_only if f.get("height") == h and f.get("ext") == "mp4"]
        if not candidates:
            candidates = [f for f in video_only if f.get("height") == h]

        if candidates and h not in seen_heights:
            best = candidates[0]
            seen_heights.add(h)
            selected.append({
                "format_id": best["format_id"],
                "label":     build_label(best),
                "type":      "video",
                "ext":       best.get("ext", "mp4")
            })

    if audio_only:
        audio_only_sorted = sorted(audio_only, key=lambda f: f.get("abr") or 0, reverse=True)
        best_audio = audio_only_sorted[0]
        selected.append({
            "format_id": best_audio["format_id"],
            "label":     build_label(best_audio),
            "type":      "audio",
            "ext":       best_audio.get("ext", "m4a")
        })

    return selected


# ── routes ────────────────────────────────────────────────────────────────────

@app.route("/api/info")
def api_info():
    url = request.args.get("url", "").strip()
    if not url:
        return jsonify({"success": False, "error": "Missing url parameter"}), 400

    info = run_ytdlp_json(url)
    if info is None:
        return jsonify({"success": False, "error": "yt-dlp failed. Video may be unavailable or age-restricted."}), 500

    formats_raw = info.get("formats", [])
    formats     = select_best_formats(formats_raw)

    if not formats:
        return jsonify({"success": False, "error": "No downloadable formats found."}), 500

    return jsonify({
        "success": True,
        "title":   info.get("title", "YouTube Video"),
        "formats": formats
    })


@app.route("/api/download")
def api_download():
    url       = request.args.get("url", "").strip()
    format_id = request.args.get("format_id", "").strip()

    if not url or not format_id:
        return jsonify({"success": False, "error": "Missing url or format_id"}), 400

    # Resolve direct URL via yt-dlp -g
    try:
        result = subprocess.run(
            YT_BASE_ARGS + ["-f", format_id, "-g", url],
            capture_output=True, text=True, timeout=60
        )
        if result.returncode != 0 or not result.stdout.strip():
            return jsonify({"success": False, "error": "Could not resolve download URL."}), 500

        direct_url = result.stdout.strip().splitlines()[0]
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

    # Determine extension
    ext = "mp4"
    try:
        info = run_ytdlp_json(url)
        if info:
            for f in info.get("formats", []):
                if str(f.get("format_id")) == str(format_id):
                    ext = f.get("ext", "mp4")
                    break
    except Exception:
        pass

    filename = f"nhyt_{format_id}.{ext}"

    def generate():
        req = urllib.request.Request(direct_url, headers={
            "User-Agent": "Mozilla/5.0 (Linux; Android 11) AppleWebKit/537.36"
        })
        with urllib.request.urlopen(req, timeout=300) as resp:
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                yield chunk

    return Response(
        stream_with_context(generate()),
        content_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )


@app.route("/")
def index():
    return jsonify({
        "status": "NH YT Downloader API running",
        "endpoints": ["/api/info", "/api/download"]
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
