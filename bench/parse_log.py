#!/usr/bin/env python3
"""Extract classification + token usage from one agy --output-format json log.

Replaces the whole-log greps that produced harness defects D2 and D5:
  D2 - `grep 429` matched a conversation_id containing 429, voiding a passing run.
  D5 - log_bytes was used as a cost proxy; it correlates with real cost at r=0.13.

Emits one CSV fragment on stdout:
  agy_status,input_tokens,output_tokens,thinking_tokens,cache_read_tokens,total_tokens,num_turns,duration_s,classified

`classified` is one of: ok_candidate | rate_limited | timeout | error | unparseable
"ok_candidate" means the run completed; whether it PASSED is decided by check.sh.
"""
import json, sys

RATE = ("rate_limit", "rate limit", "quota", "resource_exhausted", "resource exhausted")
TIME = ("timeout", "deadline")


def main(path: str) -> None:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            doc = json.load(fh)
    except Exception:
        print("NO_JSON,,,,,,,,unparseable")
        return

    status = str(doc.get("status", "")).strip() or "MISSING"
    usage = doc.get("usage") or {}

    # Classify from the status field only. Never from the whole log: the
    # response prose and the conversation_id are attacker-free but still
    # contain digits and words that match a naive grep.
    if status.upper() == "SUCCESS":
        classified = "ok_candidate"
    else:
        low = status.lower()
        if any(k in low for k in RATE):
            classified = "rate_limited"
        elif any(k in low for k in TIME):
            classified = "timeout"
        else:
            classified = "error"

    fields = [
        status,
        usage.get("input_tokens", ""),
        usage.get("output_tokens", ""),
        usage.get("thinking_tokens", ""),
        usage.get("cache_read_tokens", ""),
        usage.get("total_tokens", ""),
        doc.get("num_turns", ""),
        doc.get("duration_seconds", ""),
        classified,
    ]
    print(",".join(str(f) for f in fields))


if __name__ == "__main__":
    main(sys.argv[1])
