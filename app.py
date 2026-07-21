"""
NH YT Downloader - self-hosted backend v6
"""
import os
import sys
import uuid
import shutil
import tempfile

print("=== STARTUP: Python", sys.version, "===", flush=True)
print("=== PORT env:", os.environ.get("PORT", "NOT SET"), "===", flush=True)

from flask import Flask, request, jsonify, Response, stream_with_context
print("Flask OK", flush=True)

import yt_dlp
print("yt_dlp OK", flush=True)

import requests
print("requests OK", flush=True)

app = Flask(__name__)

WANTED_HEIGHTS = [1080, 720, 480, 360]
TMP_ROOT = os.path.join(tempfile.gettempdir(), "nhyt_downloads")
os.makedirs(TMP_ROOT, exist_ok=True)

COOKIES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cookies.txt")


def _base_opts():
    opts = {
        "quiet": False,
        "noplaylist": True,
        "ignore_no_formats_error": True,
        "extractor_args": {
            "youtube": {
                "player_client": ["tv_embedded", "ios", "web"],
            }
        },
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Linux; Android 13; Pixel 7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.6367.82 Mobile Safari/537.36"
            ),
        },
    }
    if os.path.isfile(COOKIES_PATH):
        opts["cookiefile"] = COOKIES_PATH
    return opts


@app.route("/")
def home():
    return jsonify(status="ok", service="NH YT Downloader API v6")


@app.route("/health")
def health():
    return "OK", 200


@app.route("/api/info")
def info():
    url = request.args.get("url")
    if not url:
        return jsonify(success=False, error="Missing url"), 400
    print("INFO:", url, flush=True)
    try:
        opts = _base_opts()
        opts["skip_download"] = True
        with yt_dlp.YoutubeDL(opts) as ydl:
            data = ydl.extract_info(url, download=False)
    except Exception as e:
        print("INFO ERROR:", str(e), flush=True)
        return jsonify(success=False, error=str(e)), 500

    title = data.get("title", "YouTube Video")

    # We deliberately don't introspect the raw formats list here — different
    # yt-dlp player clients (tv_embedded/ios/web) structure height/codec
    # fields inconsistently, which was causing false "no formats found"
    # failures. Instead we always offer standard quality selectors; yt-dlp
    # resolves the actual best-available match for THIS video at download
    # time, gracefully falling back to a lower height if needed.
    result_formats = []
    for h in WANTED_HEIGHTS:
        result_formats.append({
            "label": "Video {}p (MP4)".format(h),
            "format_id": "best[height<={0}]/bestvideo[height<={0}]+bestaudio/best".format(h),
            "type": "merge",
        })
    result_formats.append({
        "label": "Audio Only (M4A)",
        "format_id": "bestaudio/best",
        "type": "audio",
    })

    print("INFO OK:", title, flush=True)
    return jsonify(success=True, title=title, formats=result_formats)


@app.route("/api/download")
def download():
    url = request.args.get("url")
    format_id = request.args.get("format_id")
    if not url or not format_id:
        return jsonify(success=False, error="Missing url or format_id"), 400

    print("DOWNLOAD:", format_id, flush=True)

    if "+" not in format_id and format_id != "bestaudio/best":
        try:
            opts = _base_opts()
            opts["format"] = format_id
            with yt_dlp.YoutubeDL(opts) as ydl:
                resolved = ydl.extract_info(url, download=False)
        except Exception as e:
            print("DOWNLOAD ERROR:", str(e), flush=True)
            return jsonify(success=False, error=str(e)), 500

        direct_url = resolved.get("url")
        req_headers = resolved.get("http_headers", {}) or {}
        if direct_url:
            ext = resolved.get("ext", "mp4")

            def generate_proxy():
                with requests.get(direct_url, headers=req_headers,
                                  stream=True, timeout=60) as r:
                    r.raise_for_status()
                    for chunk in r.iter_content(chunk_size=65536):
                        if chunk:
                            yield chunk

            return Response(
                stream_with_context(generate_proxy()),
                mimetype="application/octet-stream",
                headers={"Content-Disposition":
                         'attachment; filename="nhyt_download.{}"'.format(ext)},
            )

    job_dir = os.path.join(TMP_ROOT, uuid.uuid4().hex)
    os.makedirs(job_dir, exist_ok=True)
    outtmpl = os.path.join(job_dir, "out.%(ext)s")
    is_audio_only = format_id.strip() == "bestaudio/best"

    opts = _base_opts()
    opts["format"] = format_id
    opts["outtmpl"] = outtmpl
    if is_audio_only:
        opts["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "m4a"}]
    else:
        opts["merge_output_format"] = "mp4"

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
    except Exception as e:
        shutil.rmtree(job_dir, ignore_errors=True)
        print("MERGE ERROR:", str(e), flush=True)
        return jsonify(success=False, error=str(e)), 500

    produced = [f for f in os.listdir(job_dir) if f.startswith("out.")]
    if not produced:
        shutil.rmtree(job_dir, ignore_errors=True)
        return jsonify(success=False, error="Merge produced no output file"), 500

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
        headers={"Content-Disposition":
                 'attachment; filename="nhyt_download.{}"'.format(ext)},
    )


print("=== App object ready ===", flush=True)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print("Running on port", port, flush=True)
    app.run(host="0.0.0.0", port=port)
