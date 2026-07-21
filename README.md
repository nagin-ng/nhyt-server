# NH YT Downloader — Backend

Self-hosted replacement for RapidAPI/cobalt.tools. Uses `yt-dlp` to fetch
YouTube video info and stream downloads. Your Android app hits this server
instead of a third-party API.

## Deploy on Railway (free tier, easiest)

1. Go to https://railway.app → sign in with GitHub.
2. Push this `nhyt-server` folder to a new GitHub repo (or use Railway's
   "Deploy from local folder" via their CLI).
3. Railway → New Project → Deploy from GitHub repo → select the repo.
4. Railway auto-detects Python + the `Procfile` and deploys it.
5. Once deployed, Railway gives you a public URL like:
   `https://your-app-name.up.railway.app`
6. Test it in a browser:
   `https://your-app-name.up.railway.app/api/info?url=https://youtu.be/dQw4w9WgXcQ`
   You should get back JSON with a title and formats list.

## Deploy on Render (also free tier)

1. https://render.com → New → Web Service → connect your GitHub repo.
2. Build command: `pip install -r requirements.txt`
3. Start command: `gunicorn app:app --bind 0.0.0.0:$PORT --timeout 120`
4. Deploy, then use the given `https://your-app.onrender.com` URL.

## After deploying

Send me the live URL (e.g. `https://your-app-name.up.railway.app`) and I'll
wire it into `DownloadService.java` in place of the RapidAPI calls.

## Notes

- Free tiers sleep after inactivity — first request after idle can take
  10–20s to "wake up". Normal, not a bug.
- yt-dlp needs to stay updated (`pip install -U yt-dlp`) since YouTube
  changes its site often — outdated yt-dlp = broken extraction. Railway/Render
  reinstall from `requirements.txt` on every deploy, so bump the version
  there periodically (or use `yt-dlp>=` as already set, which grabs latest
  compatible release on redeploy).
- This proxies (streams) the video through your server rather than handing
  back a raw YouTube URL — avoids YouTube's IP-locked signed URLs breaking
  when the phone's network differs from the server's.
