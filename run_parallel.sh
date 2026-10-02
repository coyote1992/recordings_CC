#!/usr/bin/env bash
# Record a targets CSV with N parallel workers pulling from one shared queue.
# Each worker has its own virtual display (:100+k), PulseAudio sink (rec<k>) and work dir.
# Already-recorded lessons are skipped, so this is safe to re-run after an interruption.
# A new worker for slot k waits until any older worker in slot k has finished (see WAVE).
# Usage: CC_EMAIL=... CC_PASSWORD=... [WAVE=_b] ./run_parallel.sh [N] [targets.csv]
set -euo pipefail
cd "$(dirname "$0")"
N=${1:-4}; TARGETS=${2:-targets_run.csv}; WAVE=${WAVE:-}
CLAIMS=/tmp/claims${WAVE}
./setup_env.sh >/dev/null
export PULSE_SERVER=unix:/tmp/pulse.sock
mkdir -p logs; rm -f logs/ALL_DONE; rm -rf "$CLAIMS"; mkdir -p "$CLAIMS"
# lessons still being recorded by older workers count as claimed
python3 - "$TARGETS" "$CLAIMS" <<'PY'
import csv,glob,os,re,sys
from pathlib import Path
stems={Path(f).name.split(".attempt")[0] for f in glob.glob("/tmp/recording_work_w*/*.raw.mkv")}
for r in csv.DictReader(open(sys.argv[1])):
    if Path(r["filename"]).stem in stems:
        os.makedirs(Path(sys.argv[2])/re.sub(r"[^a-z0-9]+","-",r["filename"].lower()).strip("-")[:120],exist_ok=True)
PY
pids=()
for ((k=0; k<N; k++)); do
  D=$((100+k))
  if ! pgrep -f "Xvfb :$D " >/dev/null; then
    rm -f /tmp/.X$D-lock /tmp/.X11-unix/X$D   # stale files from before a container restart
    setsid nohup Xvfb :$D -screen 0 2000x1400x24 -nolisten tcp >/dev/null 2>&1 & sleep 1
  fi
  pactl list short sinks | grep -q "rec$k" || pactl load-module module-null-sink sink_name=rec$k >/dev/null
  (
    while pgrep -f "results_w$k.csv" >/dev/null; do sleep 15; done   # older worker in this slot
    DISPLAY=:$D PULSE_SINK=rec$k RECORD_WORK_DIR=/tmp/recording_work_w$k \
      python3 record_batch.py --targets "$TARGETS" --claims "$CLAIMS" --results "logs/results_w$k${WAVE}.csv" \
      > "logs/worker$k${WAVE}.log" 2>&1
  ) &
  pids+=($!)
  sleep 3
done
for p in "${pids[@]}"; do wait "$p" || true; done
# also wait for any recorder started by hand / by an older wave
while pgrep -f "python3 record_batch" >/dev/null; do sleep 20; done
touch logs/ALL_DONE
echo "ALL WORKERS FINISHED"
