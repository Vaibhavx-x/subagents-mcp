from pkg.server import build_url
from pkg.health import report


def test_build_url_default():
    assert build_url() == "http://localhost:8080"


def test_health():
    assert report()["port"] == 8080
