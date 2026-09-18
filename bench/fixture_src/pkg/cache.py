"""A small in-memory cache keyed by a specification mapping."""


def _key(spec):
    return tuple(spec.items())


class SpecCache:
    def __init__(self):
        self._store = {}
        self.hits = 0
        self.misses = 0

    def put(self, spec, value):
        self._store[_key(spec)] = value

    def get(self, spec):
        k = _key(spec)
        if k in self._store:
            self.hits += 1
            return self._store[k]
        self.misses += 1
        return None

    def size(self):
        return len(self._store)
