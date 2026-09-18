"""Fake server bootstrap."""

from pkg.config import fetch_cfg


def build_url(cfg_path=None):
    cfg = fetch_cfg(cfg_path)
    return "http://{host}:{port}".format(**cfg)


def is_debug(cfg_path=None):
    # fetch_cfg gives us the merged view
    return fetch_cfg(cfg_path)["debug"]
