"""
NH YT Downloader — self-hosted backend
---------------------------------------
Replaces RapidAPI / cobalt.tools with your own yt-dlp powered API.

Endpoints:
  GET /api/info?url=<youtube_url>
      -> { success, title, formats: [{label, format_id, type}] }

  GET /api/download?url=<youtube_url>&format_id=<id>
      -> streams the file back (server proxies it — avoids YouTube's
         IP-locked direct URLs failing when the phone's IP differs
         from the server that resolved them)

  GET /
      -> health check
"""

from flask import Flask, request, jsonify, Response, stream_with_context
import yt_dlp
import requests

app = Flask(__name__)

WANTED_HEIGHTS = [1080, 720, 480, 360]


def extract_info(url):
    opts = {
        "quiet": True,
        "noplaylist": True,
        "skip_download": True,
        # Helps avoid some bot-detection edge cases on YouTube's side
        "extractor_args": {"youtube": {"player_client": ["android", "web"]}},
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)


@app.route("/")
def home():
    return jsonify(status="ok", service="NH YT Downloader API")


@app.route("/api/info")
def info():
    url = request.args.get("url")
    if not url:
        return jsonify(success=False, error="Missing url"), 400

    try:
        data = extract_info(url)
    except Exception as e:
        return jsonify(success=False, error=str(e)), 500

    title = data.get("title", "YouTube Video")
    all_formats = data.get("formats", [])

    # Progressive (video+audio in one file) mp4 formats at common heights
    picked = {}
    for f in all_formats:
        h = f.get("height")
        vcodec = f.get("vcodec")
        acodec = f.get("acodec")
        ext = f.get("ext")
        if h in WANTED_HEIGHTS and vcodec not in (None, "none") \
                and acodec not in (None, "none") and ext == "mp4":
            if h not in picked:
                picked[h] = f["format_id"]

    result_formats = []
    for h in WANTED_HEIGHTS:
        if h in picked:
            result_formats.append({
                "label": f"Video {h}p (MP4)",
                "format_id": picked[h],
                "type": "video",
            })

    # Best audio-only track
    audio_formats = [f for f in all_formats
                      if f.get("vcodec") in (None, "none")
                      and f.get("acodec") not in (None, "none")]
    if audio_formats:
        best_audio = max(audio_formats, key=lambda f: f.get("abr") or 0)
        result_formats.append({
            "label": f"Audio Only ({(best_audio.get('ext') or 'm4a').upper()})",
            "format_id": best_audio["format_id"],
            "type": "audio",
        })

    if not result_formats:
        return jsonify(success=False, error="No downloadable formats found"), 404

    return jsonify(success=True, title=title, formats=result_formats)


@app.route("/api/download")
def download():
    url = request.args.get("url")
    format_id = request.args.get("format_id")
    if not url or not format_id:
        return jsonify(success=False, error="Missing url or format_id"), 400

    try:
        opts = {"quiet": True, "noplaylist": True, "format": format_id}
        with yt_dlp.YoutubeDL(opts) as ydl:
            resolved = ydl.extract_info(url, download=False)
    except Exception as e:
        return jsonify(success=False, error=str(e)), 500

    direct_url = resolved.get("url")
    req_headers = resolved.get("http_headers", {}) or {}
    if not direct_url:
        return jsonify(success=False, error="Could not resolve stream URL"), 500

    ext = resolved.get("ext", "mp4")
    filename = f"nhyt_download.{ext}"

    def generate():
        with requests.get(direct_url, headers=req_headers, stream=True, timeout=60) as r:
            r.raise_for_status()
            for chunk in r.iter_content(chunk_size=65536):
                if chunk:
                    yield chunk

    return Response(
        stream_with_context(generate()),
        mimetype="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
