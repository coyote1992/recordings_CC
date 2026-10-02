#!/usr/bin/env bash
# Record a targets CSV with N parallel workers pulling from one shared queue.
# Each worker has its own virtual display (:100+k), PulseAudio sink (rec<k>) and work dir.
# Already-recorded lessons are skipped, so this is safe to re-run after an interruption.
# Usage: CC_EMAIL=... CC_PASSWORD=... ./run_parallel.sh [N] [targets.csv]
set -euo pipefail
cd "$(dirname "$0")"
N=${1:-4}; TARGETS=${2:-targets_run.csv}
./setup_env.sh >/dev/null
export PULSE_SERVER=unix:/tmp/pulse.sock
mkdir -p logs; rm -f logs/ALL_DONE; rm -rf /tmp/claims; mkdir -p /tmp/claims
pids=()
for ((k=0; k<N; k++)); do
  D=$((100+k))
  pgrep -f "Xvfb :$D" >/dev/null || { setsid nohup Xvfb :$D -screen 0 2000x1400x24 -nolisten tcp >/dev/null 2>&1 & sleep 1; }
  pactl list short sinks | grep -q "rec$k" || pactl load-module module-null-sink sink_name=rec$k >/dev/null
  DISPLAY=:$D PULSE_SINK=rec$k RECORD_WORK_DIR=/tmp/recording_work_w$k \
    python3 record_batch.py --targets "$TARGETS" --claims /tmp/claims --results "logs/results_w$k.csv" \
    > "logs/worker$k.log" 2>&1 &
  pids+=($!)
  sleep 8   # stagger logins
done
for p in "${pids[@]}"; do wait "$p" || true; done
touch logs/ALL_DONE
echo "ALL WORKERS FINISHED"
