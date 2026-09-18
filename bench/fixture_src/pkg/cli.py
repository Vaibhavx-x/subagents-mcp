"""Command line entry point."""

import sys

from pkg.config import fetch_cfg
from pkg.server import build_url


def main(argv=None):
    argv = argv or sys.argv[1:]
    path = argv[0] if argv else None
    cfg = fetch_cfg(path)
    print(build_url(path), "debug=", cfg["debug"])
    return 0
