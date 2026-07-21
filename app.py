"""
NH YT Downloader — self-hosted backend (v2, all resolutions)
--------------------------------------------------------------
- 360p (progressive, video+audio already combined by YouTube): streamed
  directly, no disk write, very fast.
- 720p / 1080p / audio-only: YouTube serves these as SEPARATE video-only and
  audio-only streams. yt-dlp downloads both and merges them with ffmpeg into
  one file, which is then streamed back and cleaned up.

Requires ffmpeg on the server (see Dockerfile).

Endpoints:
  GET /api/info?url=<youtube_url>
      -> { success, title, formats: [{label, format_id, type}] }
      format_id is either a plain yt-dlp format id (progressive, e.g. "18")
      or a yt-dlp format *selector* string (merge, e.g. "137+bestaudio/best").

  GET /api/download?url=<youtube_url>&format_id=<id_or_selector>
      -> streams the file back.

  GET /
      -> health check
"""

import os
import uuid
import shutil
import tempfile
from flask import Flask, request, jsonify, Response, stream_with_context
import yt_dlp
import requests

app = Flask(__name__)

WANTED_HEIGHTS = [1080, 720, 480, 360]
TMP_ROOT = os.path.join(tempfile.gettempdir(), "nhyt_downloads")
os.makedirs(TMP_ROOT, exist_ok=True)

# ── Bot-bypass base options ──────────────────────────────────────────────────
# YouTube blocks plain server IPs as bots. Using android+web player clients
# bypasses the "Sign in to confirm you're not a bot" error without cookies.
YDL_BASE = {
    "quiet": True,
    "noplaylist": True,
    "extractor_args": {
        "youtube": {
            "player_client": ["android", "web"],
        }
    },
    "http_headers": {
        "User-Agent": (
            "Mozilla/5.0 (Linux; Android 11; Pixel 5) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Mobile Safari/537.36"
        ),
    },
}


def extract_info(url):
    opts = {**YDL_BASE, "skip_download": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)


@app.route("/")
def home():
    return jsonify(status="ok", service="NH YT Downloader API v2")


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

    # Progressive mp4 (video+audio combined) — YouTube reliably only offers
    # this at up to 360p.
    progressive = {}
    for f in all_formats:
        h = f.get("height")
        if h in WANTED_HEIGHTS and f.get("vcodec") not in (None, "none") \
                and f.get("acodec") not in (None, "none") and f.get("ext") == "mp4":
            if h not in progressive:
                progressive[h] = f["format_id"]

    # Video-only formats (for merging with best audio) at each wanted height
    video_only = {}
    for f in all_formats:
        h = f.get("height")
        if h in WANTED_HEIGHTS and f.get("vcodec") not in (None, "none") \
                and f.get("acodec") in (None, "none"):
            # prefer mp4/avc for compatibility & faster remux
            if h not in video_only or (f.get("ext") == "mp4" and video_only[h][1] != "mp4"):
                video_only[h] = (f["format_id"], f.get("ext"))

    audio_only = [f for f in all_formats
                  if f.get("vcodec") in (None, "none") and f.get("acodec") not in (None, "none")]
    has_audio = len(audio_only) > 0
    best_audio = max(audio_only, key=lambda f: f.get("abr") or 0) if audio_only else None

    result_formats = []
    for h in WANTED_HEIGHTS:
        if h in progressive:
            result_formats.append({
                "label": f"Video {h}p (MP4)",
                "format_id": progressive[h],
                "type": "progressive",
            })
        elif h in video_only and has_audio:
            vid_id = video_only[h][0]
            result_formats.append({
                "label": f"Video {h}p (MP4)",
                "format_id": f"{vid_id}+bestaudio/best",
                "type": "merge",
            })

    if best_audio:
        result_formats.append({
            "label": f"Audio Only ({(best_audio.get('ext') or 'm4a').upper()})",
            "format_id": "bestaudio/best",
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

    # ── Fast path: plain format id with no "+" -> progressive, proxy the
    #    direct URL without touching disk.
    if "+" not in format_id and format_id != "bestaudio/best":
        try:
            opts = {**YDL_BASE, "format": format_id}
            with yt_dlp.YoutubeDL(opts) as ydl:
                resolved = ydl.extract_info(url, download=False)
        except Exception as e:
            return jsonify(success=False, error=str(e)), 500

        direct_url = resolved.get("url")
        req_headers = resolved.get("http_headers", {}) or {}
        if direct_url:
            ext = resolved.get("ext", "mp4")

            def generate_proxy():
                with requests.get(direct_url, headers=req_headers, stream=True, timeout=60) as r:
                    r.raise_for_status()
                    for chunk in r.iter_content(chunk_size=65536):
                        if chunk:
                            yield chunk

            return Response(
                stream_with_context(generate_proxy()),
                mimetype="application/octet-stream",
                headers={"Content-Disposition": f'attachment; filename="nhyt_download.{ext}"'},
            )
        # fall through to merge path if no direct url was resolvable

    # ── Merge path: download video+audio (or best audio) and mux with ffmpeg,
    #    then stream the resulting file and clean up.
    job_dir = os.path.join(TMP_ROOT, uuid.uuid4().hex)
    os.makedirs(job_dir, exist_ok=True)
    outtmpl = os.path.join(job_dir, "out.%(ext)s")

    is_audio_only = format_id.strip() == "bestaudio/best"

    opts = {
        **YDL_BASE,
        "format": format_id,
        "outtmpl": outtmpl,
    }
    if is_audio_only:
        opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "m4a",
        }]
    else:
        opts["merge_output_format"] = "mp4"

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
    except Exception as e:
        shutil.rmtree(job_dir, ignore_errors=True)
        return jsonify(success=False, error=str(e)), 500

    produced = [f for f in os.listdir(job_dir) if f.startswith("out.")]
    if not produced:
        shutil.rmtree(job_dir, ignore_errors=True)
        return jsonify(success=False, error="Merge/download produced no output file"), 500

    out_path = os.path.join(job_dir, produced[0])
    ext = produced[0].split(".")[-1]

    def generate_file():
        try:
            with open(out_path, "rb") as f:
                while True:
                    chunk = f.read(65536)
                    if not chunk:
                        break
                    yield chunk
        finally:
            shutil.rmtree(job_dir, ignore_errors=True)

    return Response(
        stream_with_context(generate_file()),
        mimetype="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="nhyt_download.{ext}"'},
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
