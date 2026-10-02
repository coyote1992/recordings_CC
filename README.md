# recordings_CC

Batch screen-recorder for Crafting Cases (Thinkific + Wistia) lesson videos.

For each row in `targets.csv` it opens the lesson in headed Chrome (Chromium-based, H.264 capable)
at 1920×1080 via Playwright, presses play, records the browser viewport and the audio while the
video plays to the end, encodes an H.264/AAC MP4, verifies it, and logs the result to `results.csv`
(`url,filename,status,error`). Failures are retried once.

```bash
./setup_env.sh                                   # Playwright, Chrome, ffmpeg, Xvfb :99, PulseAudio sink
export DISPLAY=:99 CC_EMAIL=you@example.com CC_PASSWORD=...   # credentials are never stored in the repo
python3 record_batch.py                          # record everything in targets.csv -> recordings/
python3 record_batch.py --limit-seconds 60       # quick smoke test
python3 record_batch.py --discover               # write targets_all.csv with every lesson in the course
```

Verification (per file): readable by ffprobe, H.264 1920×1080 video + AAC audio, duration matches the
Wistia video length, decodes with zero ffmpeg errors, audio is not silent.
Output is kept under `MAX_MB` (default 95) because GitHub rejects files over 100 MB.

Notes from getting this to work in the sandbox:
- Stock Chromium builds cannot decode H.264/AAC (what Wistia serves), so Chrome is used.
- Requests go through Playwright's request client with retries (Chromium's own proxy connections
  intermittently fail in the sandbox); trackers/analytics are blocked.
- With the sync Playwright API never `time.sleep()` in the page flow; use `page.wait_for_timeout()`.
