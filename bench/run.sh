#!/usr/bin/env bash
# Usage: ./run.sh <config A|B|C|D> <task t1|t2|t3> [repeat_idx]
# Resets the fixture repo, runs the agent, times it, checks the result, appends to results.csv
set -u
BENCH="$(cd "$(dirname "$0")" && pwd)"
CFG="${1:?config A|B|C|D}"
TASK="${2:?task t1|t2|t3}"
REP="${3:-1}"

# Effort is encoded in the model string (see `agy models`), so there is no
# separate --effort flag here. LEVEL is recorded for the report only.
case "$CFG" in
  A) MODEL="gemini-3.8-flash-low";    LEVEL="low"    ;;
  B) MODEL="gemini-3.8-flash-medium"; LEVEL="medium" ;;
  C) MODEL="gemini-3.8-flash-high";   LEVEL="high"   ;;
  D) MODEL="gemini-3.7-flash-medium"; LEVEL="medium" ;;
  *) echo "unknown config $CFG"; exit 2 ;;
esac

TIMEOUT="${TIMEOUT:-420}"
AGY="${AGY_PATH:-agy}"
PY="${PY_PATH:-python}"
AGY_ARGS=(
  --model "$MODEL"
  --dangerously-skip-permissions
  --add-dir "$BENCH/fixture_repo"
  --output-format json
  --print-timeout "${TIMEOUT}s"
  --print "$(cat "$BENCH/tasks/$TASK/PROMPT.txt")"
)

RUN_ID="$(date +%s)-$CFG-$TASK-$REP"
LOGDIR="$BENCH/logs"; mkdir -p "$LOGDIR"
LOG="$LOGDIR/$RUN_ID.log"

EMPTY=",,,,,,,,"   # placeholder for the 9 parse_log.py columns

# ---- reset fixture repo to baseline ----
# -x is required: __pycache__ is now gitignored (defect D1 fix), and without -x
# `git clean` leaves ignored files in place, so stale bytecode would survive
# between runs and re-contaminate the greps D1 was fixed to protect.
cd "$BENCH/fixture_repo" || exit 1
git checkout -q -- . && git clean -qfdx
if [ -n "$(git status --porcelain)" ]; then
  echo "$RUN_ID,$CFG,$MODEL,$LEVEL,$TASK,$REP,0,0,dirty_repo$EMPTY" >> "$BENCH/results.csv"
  echo "ABORT: repo not clean"; exit 1
fi

# ---- run ----
START=$(date +%s%3N)
"$AGY" "${AGY_ARGS[@]}" > "$LOG" 2>&1
RC=$?
END=$(date +%s%3N)
MS=$((END-START))

# ---- classify ----
# Classification reads the JSON `status` field via parse_log.py. It must never
# grep the whole log: defect D2 was a `429` inside a conversation_id voiding a
# passing run, and the same grep also matched the model's own prose.
PARSED="$("$PY" "$BENCH/parse_log.py" "$LOG")"
CLASSIFIED="${PARSED##*,}"

case "$CLASSIFIED" in
  rate_limited|timeout|error|unparseable)
    STATUS="$CLASSIFIED"; PASSED=0 ;;
  ok_candidate)
    if bash "$BENCH/tasks/$TASK/check.sh" | grep -q PASS; then
      STATUS="ok"; PASSED=1
    else
      STATUS="fail"; PASSED=0
    fi ;;
  *)
    STATUS="error"; PASSED=0 ;;
esac

echo "$RUN_ID,$CFG,$MODEL,$LEVEL,$TASK,$REP,$PASSED,$MS,$STATUS,$PARSED" >> "$BENCH/results.csv"
echo "$RUN_ID  $STATUS  ${MS}ms  -> $LOG"
