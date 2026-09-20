"""Does a narrow `permissions.allow` rule let a headless parent call our tools?

The first probe established that MCP tools are auto-DENIED in headless mode,
with agy still reporting status SUCCESS and an empty response. Its stderr named
two escapes:

    Add an allow-rule under permissions.allow in settings.json (e.g.
    mcp(<target>)). Alternatively, re-run with --dangerously-skip-permissions
    to auto-approve all tools.

The second is a sledgehammer -- it auto-approves every tool the parent has. The
first is narrow and is the one worth documenting, but the hint does not say
what `<target>` should be. This settles it by trying the plausible spellings.

Touches ~/.gemini/antigravity-cli/settings.json. It backs the file up first,
restores it in a finally, and refuses to run if the backup cannot be written:
an allow-rule left behind would silently remove the human approval gate from
every future session on this machine.

    python bench/ab/preflight_allowrule.py
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

sys.path.insert(0, str(Path(__file__).parent))

from preflight_lib import SETTINGS, fresh_workspace, run_probe, verdict  # noqa: E402

from subagents.config import load_config  # noqa: E402

# The hint says mcp(<target>) without saying what a target is. These are the
# spellings a reader would try, cheapest and most specific first.
CANDIDATES = [
    ["mcp(subagents/propose_plan)", "mcp(subagents/execute_plan)", "mcp(subagents/collect)"],
    ["mcp(subagents)"],
    ["mcp"],
]


def main() -> int:
    if not SETTINGS.is_file():
        print(f"no settings file at {SETTINGS}; nothing to test")
        return 1

    backup = SETTINGS.with_suffix(".json.preflight-backup")
    shutil.copy2(SETTINGS, backup)
    if not backup.is_file():
        print("refusing to run: could not write a backup of settings.json")
        return 1
    print(f"backed up {SETTINGS} -> {backup}\n")

    original = SETTINGS.read_text(encoding="utf-8")
    db = load_config().db_path
    results = []

    try:
        for rules in CANDIDATES:
            settings = json.loads(original)
            settings.setdefault("permissions", {})["allow"] = rules
            SETTINGS.write_text(json.dumps(settings, indent=2), encoding="utf-8")

            root = fresh_workspace()
            print(f"--- allow-rule {rules} ...", flush=True)
            result = run_probe(str(rules), root, skip_permissions=False, db=db)
            result["verdict"] = verdict(result)
            result["rules"] = rules
            results.append(result)
            print(f"    {result['verdict']}")
            print(f"    denied={[d.get('display_name') for d in result['denied_actions']]} "
                  f"plans={result['plans_created']} file={result['file_written']} "
                  f"in_tok={result['input_tokens']} {result['elapsed_s']}s\n")

            if result["verdict"].startswith("OK"):
                print(f"WORKS: {rules}\nstopping here rather than spending on the rest\n")
                break
    finally:
        SETTINGS.write_text(original, encoding="utf-8")
        restored = SETTINGS.read_text(encoding="utf-8")
        assert restored == original, "settings.json was NOT restored -- fix by hand"
        backup.unlink(missing_ok=True)
        print(f"restored {SETTINGS}")

    out = REPO / "bench" / "ab" / "preflight_allowrule_result.json"
    out.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"written: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
