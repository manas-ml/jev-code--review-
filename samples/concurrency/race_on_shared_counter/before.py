import threading

_lock = threading.Lock()
_cache = {}

def get_or_compute(key, compute_fn):
    with _lock:
        if key not in _cache:
            _cache[key] = compute_fn(key)
        return _cache[key]
