"""Parallel fan-out, SEC rate pacing, and crash-safe caching.

These pin the *new* fast/robust path so it can never silently regress into the
old behaviour: results must stay in input order (determinism), the aggregate
network rate must stay paced (SEC fair-access), and a cache hit must skip the
network while a cache write must be atomic.
"""
import json
import os
import threading
import time
import urllib.request

import pytest

from landmine._parallel import parallel_map
from landmine.data.provider import (HttpCompanyFactsProvider, _RateLimiter,
                                     _atomic_write_json)


# ---- parallel_map ----------------------------------------------------------

def test_parallel_map_preserves_order_and_overlaps():
    def slow(x):
        time.sleep(0.05)
        return x * x

    items = list(range(12))
    expected = [x * x for x in items]
    assert parallel_map(slow, items, max_workers=1) == expected   # sequential
    t0 = time.monotonic()
    out = parallel_map(slow, items, max_workers=6)
    elapsed = time.monotonic() - t0
    assert out == expected                       # order preserved despite threads
    assert elapsed < 0.05 * len(items)           # work actually overlapped


def test_parallel_map_single_item_runs_inline():
    calls = []
    parallel_map(lambda x: calls.append(threading.current_thread().name),
                 [1], max_workers=8)
    assert calls == ["MainThread"]               # no pool spun up for one item


def test_parallel_map_propagates_first_exception():
    def f(x):
        if x == 3:
            raise ValueError("boom")
        return x

    with pytest.raises(ValueError):
        parallel_map(f, range(6), max_workers=4)


# ---- rate limiter ----------------------------------------------------------

def test_rate_limiter_bounds_aggregate_rate_across_threads():
    interval = 0.05
    lim = _RateLimiter(max_per_second=1.0 / interval)
    n = 8
    t0 = time.monotonic()
    threads = [threading.Thread(target=lim.acquire) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.monotonic() - t0
    # n slots spaced `interval` apart => the last fires no earlier than
    # (n-1)*interval, regardless of how many threads raced for the lock.
    assert elapsed >= (n - 1) * interval * 0.8


def test_rate_limiter_zero_is_noop():
    lim = _RateLimiter(max_per_second=0)
    t0 = time.monotonic()
    for _ in range(1000):
        lim.acquire()
    assert time.monotonic() - t0 < 0.1


# ---- caching ---------------------------------------------------------------

def _ua():
    return "Test Harness test@example.com"


def test_cache_hit_skips_network(tmp_path, monkeypatch):
    cik = "0000000123"
    doc = {"facts": {"dei": {}}}
    (tmp_path / f"CIK{cik}.json").write_text(json.dumps(doc), encoding="utf-8")

    def boom(*a, **k):
        raise AssertionError("network must not be touched on a cache hit")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    p = HttpCompanyFactsProvider(user_agent=_ua(), cache_dir=str(tmp_path))
    assert p._fetch_json(cik) == doc


def test_network_miss_writes_atomic_complete_cache(tmp_path, monkeypatch):
    cik = "0000000777"
    doc = {"facts": {"us-gaap": {"x": 1}}}

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps(doc).encode("utf-8")

    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: FakeResp())
    # _RateLimiter(0) => no pacing, so the test doesn't sleep.
    p = HttpCompanyFactsProvider(user_agent=_ua(), cache_dir=str(tmp_path),
                                 limiter=_RateLimiter(0))
    assert p._fetch_json(cik) == doc
    cpath = tmp_path / f"CIK{cik}.json"
    assert json.loads(cpath.read_text(encoding="utf-8")) == doc
    # no torn temp file left behind
    assert not any(n.startswith(".tmp-") for n in os.listdir(tmp_path))


def test_atomic_write_leaves_no_temp_on_failure(tmp_path):
    path = str(tmp_path / "out.json")

    class Unserializable:
        pass

    with pytest.raises(TypeError):
        _atomic_write_json(path, {"bad": Unserializable()})
    assert not os.path.exists(path)
    assert os.listdir(tmp_path) == []            # temp file cleaned up
