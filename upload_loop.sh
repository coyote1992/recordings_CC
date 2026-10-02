#!/usr/bin/env bash
# Every minute: merge worker results into results.csv, then commit + push any new videos.
# Stops after the workers are finished (logs/ALL_DONE exists) and one final push.
cd "$(dirname "$0")"
BRANCH=claude/keen-gates-j8rqov
push_once() {
  python3 - <<'PY'
import csv,glob
rows={}
for p in ["results.csv"]+sorted(glob.glob("logs/results_w*.csv")):
    try:
        for r in csv.DictReader(open(p)): rows[r["url"]]=r
    except FileNotFoundError: pass
w=csv.DictWriter(open("results.csv","w",newline=""),fieldnames=["url","filename","status","error"])
w.writeheader(); w.writerows(rows.values())
PY
  git add recordings results.csv
  git diff --cached --quiet && return 0
  n=$(git diff --cached --name-only | grep -c '\.mp4$' || true)
  # only commit when there are new videos (or on the final pass), not for every results.csv tweak
  if [ "$n" = 0 ] && [ "${FINAL:-0}" != 1 ]; then git reset -q; return 0; fi
  git commit -q -m "Add $n recorded video file(s)

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01RpMDzLcwbCYcWGvJPzyb3p"
  for d in 0 5 15 45 90; do sleep $d; git push -q -u origin "$BRANCH" 2>&1 && return 0; done
  echo "$(date +%T) push failed" >> logs/upload_errors.log
}
while true; do
  done_flag=0; [ -f logs/ALL_DONE ] && done_flag=1
  FINAL=$done_flag push_once
  [ $done_flag = 1 ] && break
  sleep 60
done
