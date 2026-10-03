# recordings_CC

Recordings of the Crafting Cases "Structure from Scratch" course (Thinkific + Wistia), made by a
batch screen-recorder: headed Chrome at 1920x1080 on a virtual display, with audio captured from a
virtual sound card, one MP4 per lesson video.

## Layout

    recordings/<NN>_<module-name>/<NN>_<lesson-title>.mp4            lesson fits in one file
    recordings/<NN>_<module-name>/<NN>_<lesson-title>_part1.mp4 ...  longer lessons, split in order

Folder number = module, file number = lesson position inside the module. Lessons are cut into parts
(no re-encode) only because GitHub rejects files over 100 MB; play the parts in order.
`results.csv` lists every lesson: `url, filename(s), status, error`.

## Contents

All 87 video lessons of the course (37.3 hours): 7 modules, 94 MP4 files (7 long lessons are split into
parts), about 3.8 GB, all 1920x1080 H.264 with audio. Not recorded because they have no video: one text
lesson ("Work in Progress", module 7) and the feedback survey. `results.csv` shows every lesson as `success`.

## Re-running

```bash
./setup_env.sh                        # Playwright, Chrome (H.264), ffmpeg, Xvfb, PulseAudio
export CC_EMAIL=you@example.com CC_PASSWORD=...        # never stored in the repo
python3 record_batch.py --discover    # -> targets_all.csv from the course JSON API
./run_parallel.sh 4 targets_run.csv   # 4 workers, shared queue, skips finished lessons
./upload_loop.sh                      # commits + pushes finished videos as they appear
```

Notes: stock Chromium can't decode Wistia's H.264, hence Chrome. Requests go through Playwright's
request client with retries (Chromium's own proxy connections flake in the sandbox). With the sync
Playwright API never `time.sleep()` in the page flow, use `page.wait_for_timeout()`.
