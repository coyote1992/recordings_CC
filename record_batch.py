#!/usr/bin/env python3
"""Batch-record Crafting Cases (Thinkific/Wistia) lesson videos to MP4 (split into <100 MB parts).

For each URL in targets.csv:
  1. open it in headed Chrome(Chromium) (kiosk, 1920x1080) on a virtual X display via Playwright
  2. click play on the lesson video and let it run until it ends
  3. record the display (== browser viewport) + audio with ffmpeg
  4. encode a final H.264/AAC MP4 under a size budget
  5. verify the file (streams, resolution, duration, decodes cleanly, audio not silent)
Failures are retried once. Results go to results.csv (url, filename, status, error).

Credentials come from the environment (never committed):
  CC_EMAIL, CC_PASSWORD
Display / audio are provided by setup_env.sh (Xvfb :99 + PulseAudio null sink "rec").

(Display is 2000x1400; the 1920x1080 browser viewport is cropped out by ffmpeg.)

Usage:
  python3 record_batch.py                       # record everything in targets.csv
  python3 record_batch.py --limit-seconds 60    # quick smoke test
  python3 record_batch.py --discover            # write targets_all.csv (every lesson in the course)
"""
import argparse
import csv
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent
OUT_DIR = ROOT / "recordings"
WORK_DIR = Path(os.environ.get("RECORD_WORK_DIR", "/tmp/recording_work"))
BASE = "https://students.craftingcases.com"
COURSE = f"{BASE}/courses/take/structure-from-scratch"
W, H = 1920, 1080
CHROMIUM = os.environ.get(
    "CHROMIUM_PATH", "/opt/google/chrome/chrome"
)
PROXY = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
DISPLAY = os.environ.get("DISPLAY", ":99")
PULSE_SERVER = os.environ.get("PULSE_SERVER", "unix:/tmp/pulse.sock")
AUDIO_SRC = os.environ.get("PULSE_SINK", "rec") + ".monitor"  # per-worker null sink
MAX_PART_MB = float(os.environ.get("MAX_PART_MB", "85"))  # GitHub rejects files > 100 MB: split into parts below this
CRF = os.environ.get("CRF", "28")
CAPTURE_FPS = os.environ.get("CAPTURE_FPS", "20")  # lower = less CPU, so 4 workers fit on 4 cores

# Third-party trackers: not needed for playback and they flake through the proxy.
BLOCK = re.compile(
    r"clarity\.ms|mxpnl|facebook|hellobar|deadlinefunnel|newrelic|google-analytics|"
    r"googletagmanager|doubleclick|cdn-cgi/challenge|smartarget"
)


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


# NOTE: with Playwright's sync API, route handlers only run while the main thread is
# inside a Playwright call, so never time.sleep() in the page flow: use pg.wait_for_timeout().

# --------------------------------------------------------------------------- browser
def route_handler(route):
    """Proxy every request through Playwright's request client with retries.

    Chromium's own connection handling through the sandbox proxy intermittently
    fails with ERR_TOO_MANY_RETRIES; the request client does not.
    """
    req = route.request
    if BLOCK.search(req.url):
        return route.abort()
    err = None
    # Some requests intermittently hang through the proxy: use a short timeout and
    # retry idempotent GETs a few times (mutating requests get a single, longer try).
    tries, timeout = (5, 20000) if req.method == "GET" else (1, 60000)
    for _ in range(tries):
        try:
            return route.fulfill(response=route.fetch(timeout=timeout, max_redirects=0))
        except Exception as e:  # noqa: BLE001
            err = e
            time.sleep(0.5)
    log("route giveup", req.url[:100], str(err)[:80])
    return route.abort()


def launch(p):
    kw = dict(
        executable_path=CHROMIUM,
        headless=False,
        args=[
            "--no-sandbox",
            "--test-type",
            "--window-position=0,0",
            f"--window-size={W},{H + 200}",
            "--disable-quic",
            "--autoplay-policy=no-user-gesture-required",
            "--hide-scrollbars",
            "--disable-infobars",
            "--noerrdialogs",
        ],
        env={**os.environ, "DISPLAY": DISPLAY, "PULSE_SERVER": PULSE_SERVER,
             "GOOGLE_API_KEY": "no", "GOOGLE_DEFAULT_CLIENT_ID": "no",
             "GOOGLE_DEFAULT_CLIENT_SECRET": "no"},
    )
    if PROXY:
        kw["proxy"] = {"server": PROXY}
    browser = p.chromium.launch(**kw)
    ctx = browser.new_context(no_viewport=True, ignore_https_errors=True)
    ctx.route("**/*", route_handler)
    return browser, ctx


def login(ctx):
    email, pw = os.environ.get("CC_EMAIL"), os.environ.get("CC_PASSWORD")
    if not email or not pw:
        sys.exit("Set CC_EMAIL and CC_PASSWORD")
    pg = ctx.new_page()
    pg.goto(f"{BASE}/users/sign_in", wait_until="domcontentloaded", timeout=60000)
    pg.fill("#user\\[email\\]", email)
    pg.fill("#user\\[password\\]", pw)
    pg.click("input[type=submit], button[type=submit]")
    pg.wait_for_url("**/enrollments", timeout=60000)
    pg.close()
    log("logged in")


def fit_viewport(ctx, pg):
    """Size the window so the page viewport is exactly WxH; return its (x, y) on screen."""
    cdp = ctx.new_cdp_session(pg)
    win = cdp.send("Browser.getWindowForTarget")["windowId"]
    for _ in range(4):
        m = pg.evaluate("[innerWidth, innerHeight, outerWidth, outerHeight, screenX, screenY]")
        iw, ih, ow, oh, sx, sy = m
        if (iw, ih) == (W, H):
            return int(sx + (ow - iw) / 2), int(sy + (oh - ih))
        cdp.send("Browser.setWindowBounds", {"windowId": win, "bounds": {
            "left": 0, "top": 0, "width": ow + (W - iw), "height": oh + (H - ih),
            "windowState": "normal"}})
        pg.wait_for_timeout(700)
    raise RuntimeError(f"cannot fit viewport to {W}x{H}: {m}")


# --------------------------------------------------------------------------- player
JS_VIDEO = """() => {
  const v = document.querySelector('video');
  if (!v) return null;
  return {t: v.currentTime, d: v.duration, ended: v.ended, paused: v.paused, rs: v.readyState};
}"""


def video_state(pg):
    for fr in pg.frames:
        try:
            s = fr.evaluate(JS_VIDEO)
        except Exception:  # noqa: BLE001
            continue
        if s and s["d"] and s["d"] == s["d"]:  # skip NaN/0 duration
            return s
    return None


def player_frame(pg):
    for fr in pg.frames:
        if "course_player" in fr.url and "play" in fr.url:
            return fr


def wait_player_iframe(pg, timeout=120):
    end = time.time() + timeout
    while time.time() < end:
        if player_frame(pg):
            return
        pg.wait_for_timeout(1000)
    raise RuntimeError("player iframe did not appear")


def wait_video(pg, timeout=90):
    end = time.time() + timeout
    while time.time() < end:
        s = video_state(pg)
        if s and s["rs"] >= 1:
            return s
        pg.wait_for_timeout(500)
    raise RuntimeError("video did not start")


def click_play(pg):
    """Click the centre of the Wistia iframe (the big play button)."""
    fr = player_frame(pg)
    if not fr:
        raise RuntimeError("player iframe not found")
    box = fr.frame_element().bounding_box()
    pg.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)


# --------------------------------------------------------------------------- ffmpeg
def start_capture(raw, off=(0, 0)):
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "x11grab", "-draw_mouse", "0", "-framerate", CAPTURE_FPS,
        "-video_size", f"{W}x{H}", "-i", f"{DISPLAY}.0+{off[0]},{off[1]}",
        "-f", "pulse", "-i", AUDIO_SRC,
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "20", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k", str(raw),
    ]
    env = {**os.environ, "PULSE_SERVER": PULSE_SERVER}
    return subprocess.Popen(cmd, stdin=subprocess.PIPE, env=env)


def stop_capture(proc):
    try:
        proc.stdin.write(b"q")
        proc.stdin.flush()
    except Exception:  # noqa: BLE001
        pass
    try:
        proc.wait(timeout=60)
    except subprocess.TimeoutExpired:
        proc.kill()


def probe(path):
    r = run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams",
             "-show_format", str(path)])
    return json.loads(r.stdout) if r.returncode == 0 and r.stdout else None


def final_encode(raw, out_tmp):
    """Normal-quality CRF encode of the raw capture (low priority so live captures keep up)."""
    cmd = ["nice", "-n", "10", "ffmpeg", "-y", "-loglevel", "error", "-i", str(raw),
           "-c:v", "libx264", "-preset", "medium", "-crf", CRF, "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "64k", "-ac", "2", "-movflags", "+faststart", str(out_tmp)]
    r = run(cmd)
    if r.returncode != 0:
        raise RuntimeError("encode failed: " + r.stderr[-300:])


def split_parts(src, dest_dir, stem, duration):
    """Cut src into as few parts as needed so every part is < MAX_PART_MB (no re-encode)."""
    size_mb = src.stat().st_size / 1e6
    if size_mb <= MAX_PART_MB:
        return [src]
    first = int(size_mb / MAX_PART_MB) + 1
    for n in range(first, first + 6):
        for old in dest_dir.glob(f"{stem}_part*.mp4"):
            old.unlink()
        r = run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(src), "-c", "copy", "-map", "0",
                 "-f", "segment", "-segment_time", f"{duration / n + 2:.1f}",
                 "-reset_timestamps", "1", "-segment_start_number", "1",
                 "-segment_format_options", "movflags=+faststart",
                 str(dest_dir / f"{stem}_part%d.mp4")])
        parts = sorted(dest_dir.glob(f"{stem}_part*.mp4"),
                       key=lambda p: int(re.search(r"_part(\d+)", p.name).group(1)))
        if r.returncode == 0 and parts and all(p.stat().st_size / 1e6 < 97 for p in parts):
            return parts
    raise RuntimeError("could not split into parts under the size limit")


def verify(path, expected, partial=False):
    """Raise if the MP4 is not a valid, complete, 1080p video with audible audio."""
    if not path.exists() or path.stat().st_size < 100_000:
        raise RuntimeError("output missing or tiny")
    info = probe(path)
    if not info:
        raise RuntimeError("ffprobe cannot read file")
    v = [s for s in info["streams"] if s["codec_type"] == "video"]
    a = [s for s in info["streams"] if s["codec_type"] == "audio"]
    if not v or v[0]["codec_name"] != "h264":
        raise RuntimeError("no h264 video stream")
    if (v[0]["width"], v[0]["height"]) != (W, H):
        raise RuntimeError(f"resolution {v[0]['width']}x{v[0]['height']}")
    if not a:
        raise RuntimeError("no audio stream")
    dur = float(info["format"]["duration"])
    if not partial and not (expected - 3 <= dur <= expected + 90):
        raise RuntimeError(f"duration {dur:.1f}s vs video {expected:.1f}s")
    dec = run(["ffmpeg", "-v", "error", "-i", str(path), "-f", "null", "-"])
    if dec.returncode != 0 or dec.stderr.strip():
        raise RuntimeError("decode errors: " + dec.stderr[:200])
    vol = run(["ffmpeg", "-i", str(path), "-vn", "-af", "volumedetect", "-f", "null", "-"])
    m = re.search(r"mean_volume: (-?[\d.]+) dB", vol.stderr)
    if not m or float(m.group(1)) < -70:
        raise RuntimeError("audio is silent")
    return dur, path.stat().st_size / 1e6, float(m.group(1))


# --------------------------------------------------------------------------- one target
def record_one(ctx, url, rel_name, limit_seconds=None, attempt=1):
    """Record one lesson; returns (duration_s, total_mb, mean_vol_db, [final files relative to recordings/])."""
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    stem = Path(rel_name).stem
    raw = WORK_DIR / f"{stem}.attempt{attempt}.raw.mkv"  # never overwrite an earlier capture
    pg = ctx.new_page()
    cap = None
    try:
        pg.goto(url, wait_until="domcontentloaded", timeout=90000)
        wait_player_iframe(pg)
        pg.wait_for_timeout(3000)
        off = fit_viewport(ctx, pg)
        log(f"player iframe ready, viewport offset {off}")
        cap = start_capture(raw, off)
        pg.wait_for_timeout(1500)  # let ffmpeg settle
        click_play(pg)
        started = time.time()
        st = wait_video(pg)  # the <video> only exists once playback is requested
        duration = st["d"]
        log(f"playing, video length {duration:.1f}s")
        deadline = started + duration * 1.5 + 120
        end_at = started + limit_seconds if limit_seconds else None
        last_t, stall_since, max_t = -1, time.time(), 0.0
        while True:
            s = video_state(pg)
            if s is None and max_t >= duration - 5:
                break  # player torn down right after the end
            if s:
                max_t = max(max_t, s["t"])
                # After the last frame the player rewinds to 0, so "played to the end,
                # then back near 0" counts as ended too.
                if s["ended"] or s["t"] >= s["d"] - 0.3 or (max_t >= s["d"] - 5 and s["t"] < 2):
                    break
                if s["t"] > last_t + 0.2:
                    last_t, stall_since = s["t"], time.time()
                elif s["paused"] and time.time() - started < 15:
                    click_play(pg)  # first click may have been swallowed
                    pg.wait_for_timeout(2000)
                elif time.time() - stall_since > 60:
                    raise RuntimeError(f"playback stalled at {s['t']:.0f}s")
            if end_at and time.time() >= end_at:
                break
            if time.time() > deadline:
                raise RuntimeError("timeout waiting for video to end")
            pg.wait_for_timeout(1000)
        pg.wait_for_timeout(1500)
    finally:
        if cap:
            stop_capture(cap)
        pg.close()
    log("capture done, encoding")
    full = WORK_DIR / f"{stem}.full.mp4"
    final_encode(raw, full)
    dur, mb, vol = verify(full, duration, partial=bool(limit_seconds))
    # split in the work dir, verify every part, then atomically move into the repo folder
    stage = WORK_DIR / f"{stem}.stage"
    stage.mkdir(exist_ok=True)
    parts = split_parts(full, stage, stem, dur)
    if parts == [full]:
        staged = [stage / f"{stem}.mp4"]
        shutil.move(str(full), str(staged[0]))
    else:
        staged = parts
        full.unlink()
        for p in staged:
            verify(p, 0, partial=True)
    dest = OUT_DIR / rel_name
    dest.parent.mkdir(parents=True, exist_ok=True)
    finals = []
    for p in staged:
        tgt = dest.parent / p.name
        shutil.move(str(p), str(tgt) + ".tmp")
        os.replace(str(tgt) + ".tmp", tgt)  # atomic: the uploader never sees a half-written file
        finals.append(str(tgt.relative_to(OUT_DIR)))
    shutil.rmtree(stage, ignore_errors=True)
    raw.unlink(missing_ok=True)
    return dur, mb, vol, finals


def already_done(rel_name):
    dest = OUT_DIR / rel_name
    stem = dest.stem
    return dest.exists() or any(dest.parent.glob(f"{stem}_part1.mp4"))


# --------------------------------------------------------------------------- discover
def slug(s, n=60):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:n]


def discover(ctx, dest):
    """Build the target list from the course-player JSON API: one folder per module."""
    d = ctx.request.get(f"{BASE}/api/course_player/v2/courses/structure-from-scratch",
                        timeout=60000).json()
    contents = {c["id"]: c for c in d["contents"]}
    rows = []
    for mi, ch in enumerate(sorted(d["chapters"], key=lambda c: c["position"]), 1):
        folder = f"{mi:02d}_{slug(ch['name'])}"
        for li, cid in enumerate(ch["content_ids"], 1):
            c = contents[cid]
            if c.get("display_name") != "Video":
                continue
            secs = (c.get("meta_data") or {}).get("duration_in_seconds") or 0
            rows.append([f"{BASE}/courses/take/structure-from-scratch/lessons/{c['slug']}",
                         f"{folder}/{li:02d}_{slug(c['name'])}.mp4", secs, ch["name"], c["name"]])
    with open(dest, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["url", "filename", "seconds", "module", "title"])
        w.writerows(rows)
    log(f"wrote {len(rows)} video lessons to {dest}")


# --------------------------------------------------------------------------- main
def claim(name, claims_dir):
    """Atomically claim a target so parallel workers never record the same lesson."""
    if not claims_dir:
        return True
    try:
        os.makedirs(Path(claims_dir) / slug(name, 120))
        return True
    except FileExistsError:
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--targets", default=str(ROOT / "targets.csv"))
    ap.add_argument("--results", default=str(ROOT / "results.csv"))
    ap.add_argument("--limit-seconds", type=float, default=None)
    ap.add_argument("--discover", action="store_true")
    ap.add_argument("--claims", default=None, help="shared dir: workers claim targets from one queue")
    args = ap.parse_args()
    OUT_DIR.mkdir(exist_ok=True)

    if args.discover:
        with sync_playwright() as p:
            browser, ctx = launch(p)
            try:
                login(ctx)
                return discover(ctx, ROOT / "targets_all.csv")
            finally:
                browser.close()

    with open(args.targets, newline="") as f:
        targets = list(csv.DictReader(f))
    results, consecutive_failures = [], 0
    for t in targets:
        if consecutive_failures >= 3:
            log("3 failures in a row: stopping this worker (environment problem?)")
            break
        url, name = t["url"], t["filename"]
        if already_done(name) or not claim(name, args.claims):
            continue
        status, err, files = "failure", "", [name]
        for attempt in (1, 2):  # retry failures once (fresh browser each time)
            try:
                with sync_playwright() as p:
                    browser, ctx = launch(p)
                    try:
                        login(ctx)
                        dur, mb, vol, files = record_one(ctx, url, name, args.limit_seconds, attempt)
                    finally:
                        browser.close()
                log(f"OK {name}: {dur:.1f}s, {mb:.1f} MB, {len(files)} file(s), mean vol {vol} dB")
                status, err = "success", ""
                break
            except Exception as e:  # noqa: BLE001
                err = f"attempt {attempt}: " + (str(e).strip().splitlines() or ["error"])[0][:200]
                log("FAILED", name, err)
        consecutive_failures = 0 if status == "success" else consecutive_failures + 1
        results.append({"url": url, "filename": ";".join(files), "status": status, "error": err})
        with open(args.results, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["url", "filename", "status", "error"])
            w.writeheader()
            w.writerows(results)


if __name__ == "__main__":
    main()
