#!/usr/bin/env bash
# One-time setup. Run from bench/.
#
# fixture_repo/ is BUILT from fixture_src/ rather than committed directly:
#   - a nested .git inside the project repo would become a gitlink, so the
#     fixture's files would not survive a clean clone;
#   - .gitignore is committed as part of the baseline, so compiled bytecode can
#     never be tracked again (harness defect D1).
set -e
cd "$(dirname "$0")"

if [ -d fixture_repo/.git ]; then
  echo "fixture_repo already initialised (delete it to rebuild from fixture_src/)"
else
  mkdir -p fixture_repo
  cp -r fixture_src/. fixture_repo/
  cd fixture_repo
  git init -q
  git add -A
  git -c user.email=b@b -c user.name=bench commit -qm "fixture baseline"
  cd ..
  echo "fixture_repo built from fixture_src/ at baseline commit"
fi

# The bench greps compiled sources, so a stale .pyc is a correctness hazard.
if [ -n "$(git -C fixture_repo ls-files '*.pyc')" ]; then
  echo "ERROR: bytecode is tracked in fixture_repo — defect D1 has regressed" >&2
  exit 1
fi

python -c "import pytest" 2>/dev/null || echo "WARNING: pytest not installed — run: pip install pytest"
