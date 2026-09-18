import pytest

from pkg.parser import parse_duration


def test_seconds():
    assert parse_duration("90s") == 90


def test_minutes():
    assert parse_duration("5m") == 300


def test_compound():
    assert parse_duration("2h30m") == 9000


def test_malformed_raises():
    with pytest.raises(ValueError):
        parse_duration("banana")
