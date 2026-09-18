#!/usr/bin/env bash
# Runs every config x task x repeat in randomised order, serially.
# Usage: ./run_all.sh [repeats]          (default 2)
#        TASKS="t1" ./run_all.sh 5       (restrict to some tasks)
#        CONFIGS="A D" ./run_all.sh 5    (restrict to some configs)
set -u
BENCH="$(cd "$(dirname "$0")" && pwd)"
REPEATS="${1:-2}"
CONFIGS="${CONFIGS:-A B C D}"
TASKS="${TASKS:-t1 t2 t3}"

HEADER="run_id,config,model,level,task,repeat,passed,wall_ms,status,agy_status,input_tokens,output_tokens,thinking_tokens,cache_read_tokens,total_tokens,num_turns,duration_s,classified"

# Defect D3: the original wrote the header only when the file was absent. It
# already existed without one, so DictReader consumed row 1 as the header and
# report.py never ran. Check for the header itself, not for the file.
if [ ! -f "$BENCH/results.csv" ] || [ ! -s "$BENCH/results.csv" ]; then
  echo "$HEADER" > "$BENCH/results.csv"
elif ! head -1 "$BENCH/results.csv" | grep -q '^run_id,'; then
  printf '%s\n' "$HEADER" | cat - "$BENCH/results.csv" > "$BENCH/results.csv.tmp" \
    && mv "$BENCH/results.csv.tmp" "$BENCH/results.csv"
fi

JOBS=()
for c in $CONFIGS; do for t in $TASKS; do for r in $(seq 1 "$REPEATS"); do
  JOBS+=("$c $t $r")
done; done; done

echo "${#JOBS[@]} runs queued: configs=[$CONFIGS] tasks=[$TASKS] repeats=$REPEATS"

printf '%s\n' "${JOBS[@]}" | shuf | while read -r c t r; do
  echo "--- $c $t rep$r ---"
  "$BENCH/run.sh" "$c" "$t" "$r"
  sleep 5
done
