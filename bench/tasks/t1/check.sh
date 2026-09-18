#!/usr/bin/env bash
# T1 pass criteria: old name gone, new name present >= 6 times, package still imports,
# smoke tests still green, test files untouched.
set -u
cd "$(dirname "$0")/../../fixture_repo" || exit 1

git diff --quiet -- test_parser.py test_cache.py test_smoke.py || { echo "FAIL: test files modified"; exit 1; }
grep -rq --include='*.py' "fetch_cfg" pkg/ && { echo "FAIL: old name still present"; exit 1; }
N=$(grep -ro --include='*.py' "load_config" pkg/ | wc -l)
[ "$N" -ge 6 ] || { echo "FAIL: only $N occurrences of load_config (need >=6)"; exit 1; }
python -m pytest test_smoke.py -q >/dev/null 2>&1 || { echo "FAIL: smoke tests broken"; exit 1; }
echo PASS
