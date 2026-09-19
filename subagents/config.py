"""Configuration, loaded from the environment with conservative defaults.

Keys mirror `.env.example`. A `.env` file beside the repo root is read if
present, but real environment variables always win -- so a client that exports
a variable is not silently overridden by a stale file.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Measured 2026-09-18 over 33 benchmark runs: agy CLI process startup is
# 8.7-11.9s, median 9.7s, constant across models and tasks. agy's own
# `duration_seconds` excludes it, so any estimate built on that number is short
# by ~10s per worker.
SPAWN_OVERHEAD_S = 10

# Median wall-clock of a passing benchmark run (58.2s for the default model).
EXPECTED_WORKER_S = 60

# The client cancels every MCP tool call at this many seconds unless
# `timeoutSeconds` is set in mcp_config.json. Measured, not documented.
DEFAULT_TOOL_DEADLINE_S = 180


def _load_dotenv(path: Path) -> dict[str, str]:
    """Minimal .env reader. No dependency, no interpolation, no export syntax."""
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def _get(key: str, default: str, dotenv: dict[str, str]) -> str:
    return os.environ.get(key) or dotenv.get(key) or default


def _split_roots(raw: str) -> list[str]:
    """Split an allowlist.

    Uses os.pathsep (';' on Windows) rather than ':' -- a Windows path starts
    with a drive letter and a colon, so splitting on ':' would shred 'D:/x'
    into 'D' and '/x'.
    """
    return [part.strip() for part in raw.split(os.pathsep) if part.strip()]


@dataclass(frozen=True)
class Config:
    allowed_roots: tuple[Path, ...]
    db_path: Path
    model: str
    model_escalate: str
    worker_timeout_s: int
    max_parallel: int
    plan_ttl_s: int
    log_file: Path
    log_level: str
    agy_path: str
    _dotenv: dict[str, str] = field(default_factory=dict, repr=False, compare=False)


def load_config(env_file: Path | None = None) -> Config:
    dotenv = _load_dotenv(env_file if env_file is not None else REPO_ROOT / ".env")

    raw_roots = _get("SUBAGENTS_ALLOWED_ROOTS", str(REPO_ROOT), dotenv)
    roots: list[Path] = []
    for entry in _split_roots(raw_roots):
        try:
            roots.append(Path(entry).resolve())
        except OSError:
            # An unresolvable allowlist entry is dropped rather than fatal; the
            # workspace_root check below simply will not match it.
            continue

    db_path = Path(_get("SUBAGENTS_DB_PATH", "subagents.db", dotenv))
    if not db_path.is_absolute():
        db_path = REPO_ROOT / db_path

    log_file = Path(_get("SUBAGENTS_LOG_FILE", "subagents.log", dotenv))
    if not log_file.is_absolute():
        log_file = REPO_ROOT / log_file

    return Config(
        allowed_roots=tuple(roots),
        db_path=db_path,
        model=_get("SUBAGENTS_MODEL", "gemini-3.8-flash-low", dotenv),
        model_escalate=_get("SUBAGENTS_MODEL_ESCALATE", "gemini-3.7-flash-medium", dotenv),
        worker_timeout_s=int(_get("SUBAGENTS_WORKER_TIMEOUT_S", "600", dotenv)),
        max_parallel=int(_get("SUBAGENTS_MAX_PARALLEL", "4", dotenv)),
        plan_ttl_s=int(_get("SUBAGENTS_PLAN_TTL_S", "900", dotenv)),
        log_file=log_file,
        log_level=_get("SUBAGENTS_LOG_LEVEL", "INFO", dotenv),
        agy_path=_get("SUBAGENTS_AGY_PATH", "agy", dotenv),
        _dotenv=dotenv,
    )
