"""Configuration loading helpers."""

import json
import os

DEFAULTS = {"host": "localhost", "port": 8080, "debug": False}


def fetch_cfg(path=None):
    """Return the merged configuration.

    Reads JSON from *path* if given and merges it over DEFAULTS.
    """
    cfg = dict(DEFAULTS)
    if path and os.path.exists(path):
        with open(path) as fh:
            cfg.update(json.load(fh))
    return cfg
