from pkg.cache import SpecCache


def test_basic_roundtrip():
    c = SpecCache()
    c.put({"region": "eu", "tier": "gold"}, 42)
    assert c.get({"region": "eu", "tier": "gold"}) == 42


def test_repeated_lookup():
    c = SpecCache()
    spec_a = {"region": "eu", "tier": "gold"}
    spec_b = {"tier": "gold", "region": "eu"}

    c.put(spec_a, 42)

    assert c.get(spec_a) == 42
    assert c.get(spec_b) == 42
    assert c.size() == 1
    assert c.misses == 0
