"""Health reporting."""

from pkg.config import fetch_cfg


def report():
    cfg = fetch_cfg()
    return {"ok": True, "port": cfg["port"]}
