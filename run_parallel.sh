#!/usr/bin/env bash
# Record the rows of a targets CSV with N parallel workers, each with its own virtual
# display (:100+k), its own PulseAudio sink (rec<k>) and its own work dir.
# Usage: CC_EMAIL=... CC_PASSWORD=... ./run_parallel.sh [N] [targets.csv]
set -euo pipefail
cd "$(dirname "$0")"
N=${1:-3}; TARGETS=${2:-targets.csv}
./setup_env.sh >/dev/null
export PULSE_SERVER=unix:/tmp/pulse.sock
mkdir -p logs
header=$(head -1 "$TARGETS")
pids=()
for ((k=0; k<N; k++)); do
  { echo "$header"; tail -n +2 "$TARGETS" | awk -v n="$N" -v k="$k" '(NR-1)%n==k'; } > "/tmp/targets_w$k.csv"
  [ "$(wc -l < /tmp/targets_w$k.csv)" -gt 1 ] || continue
  D=$((100+k))
  pgrep -f "Xvfb :$D" >/dev/null || { setsid nohup Xvfb :$D -screen 0 2000x1400x24 -nolisten tcp >/dev/null 2>&1 & sleep 1; }
  pactl list short sinks | grep -q "rec$k" || pactl load-module module-null-sink sink_name=rec$k >/dev/null
  DISPLAY=:$D PULSE_SINK=rec$k RECORD_WORK_DIR=/tmp/recording_work_w$k \
    python3 record_batch.py --targets /tmp/targets_w$k.csv --results "logs/results_w$k.csv" \
    > "logs/worker$k.log" 2>&1 &
  pids+=($!)
  sleep 5   # stagger logins
done
for p in "${pids[@]}"; do wait "$p" || true; done
# merge per-worker results into results.csv
{ echo "url,filename,status,error"; for f in logs/results_w*.csv; do tail -n +2 "$f"; done; } > results_parallel.csv
echo "done:"; cat results_parallel.csv
