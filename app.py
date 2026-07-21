"""
NH YT Downloader - self-hosted backend v6
"""
import os
import sys
import uuid
import shutil
import tempfile
import threading
import time

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
                "player_client": ["web", "android"],
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
    # We fix the final extension up front so we can start streaming (and set
    # Content-Disposition) before the file even exists.
    final_ext = "m4a" if is_audio_only else "mp4"
    out_path = os.path.join(job_dir, "out." + final_ext)

    opts = _base_opts()
    opts["format"] = format_id
    opts["outtmpl"] = outtmpl
    if is_audio_only:
        opts["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "m4a"}]
    else:
        opts["merge_output_format"] = "mp4"

    done_event = threading.Event()
    error_holder = {}

    def run_download():
        print("MERGE: thread started for", format_id, flush=True)
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([url])
            print("MERGE: yt-dlp download() returned OK", flush=True)
        except Exception as e:
            error_holder["error"] = str(e)
            print("MERGE ERROR:", str(e), flush=True)
        finally:
            done_event.set()

    threading.Thread(target=run_download, daemon=True).start()

    # Give it a short window to fail fast (bad format string, video removed,
    # etc.) before we commit to streaming a response.
    done_event.wait(timeout=5)
    if done_event.is_set() and "error" in error_holder and not os.path.exists(out_path):
        shutil.rmtree(job_dir, ignore_errors=True)
        return jsonify(success=False, error=error_holder["error"]), 500

    def generate_tail():
        # Send response headers/first bytes ASAP so the connection doesn't
        # sit idle (some proxies drop connections with no data for too long).
        sent = 0
        wait_start = time.time()
        file_seen = False
        # Wait for yt-dlp/ffmpeg to actually create the output file
        while not os.path.exists(out_path) and not done_event.is_set():
            if time.time() - wait_start > 60:
                print("MERGE: gave up waiting for output file after 60s", flush=True)
                break
            time.sleep(0.3)

        while True:
            if os.path.exists(out_path):
                if not file_seen:
                    print("MERGE: output file appeared, streaming started", flush=True)
                    file_seen = True
                with open(out_path, "rb") as f:
                    f.seek(sent)
                    chunk = f.read(65536)
                if chunk:
                    sent += len(chunk)
                    yield chunk
                    continue
            if done_event.is_set():
                # final flush of any bytes written after our last read
                if os.path.exists(out_path):
                    with open(out_path, "rb") as f:
                        f.seek(sent)
                        rest = f.read()
                    if rest:
                        sent += len(rest)
                        yield rest
                break
            time.sleep(0.4)

        print("MERGE: stream finished, total bytes sent:", sent, flush=True)
        shutil.rmtree(job_dir, ignore_errors=True)

    print("DOWNLOAD stream starting:", format_id, flush=True)
    return Response(
        stream_with_context(generate_tail()),
        mimetype="application/octet-stream",
        headers={"Content-Disposition": 'attachment; filename="nhyt_download.{}"'.format(final_ext)},
    )


print("=== App object ready ===", flush=True)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print("Running on port", port, flush=True)
    app.run(host="0.0.0.0", port=port)
