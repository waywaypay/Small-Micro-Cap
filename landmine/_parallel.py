"""Bounded, order-preserving parallel map for I/O-bound fan-out.

The screen and the universe size-cut both fan out one independent network fetch
per name. Running them strictly back-to-back is round-trip bound (each SEC call
waits for the previous to return), so a wide sweep crawls. A small thread pool
lets those round-trips overlap, while a process-shared rate limiter (see
:mod:`landmine.data.provider`) keeps the *aggregate* request rate under SEC's
fair-access ceiling.

Results are always returned in input order, so the engine's output stays
byte-identical to the single-worker path — the determinism guarantee is
unaffected by how many workers run the fetches.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Iterable, List, TypeVar

T = TypeVar("T")
R = TypeVar("R")


def parallel_map(fn: Callable[[T], R], items: Iterable[T],
                 max_workers: int = 1) -> List[R]:
    """Map ``fn`` over ``items``, preserving input order.

    Runs sequentially when ``max_workers <= 1`` or there is at most one item, so
    the single-threaded code path (and its exception semantics) is unchanged.
    With more workers the calls overlap on a bounded pool; the first exception
    raised by any call propagates, mirroring a plain list comprehension.
    """
    items = list(items)
    if max_workers <= 1 or len(items) <= 1:
        return [fn(x) for x in items]
    with ThreadPoolExecutor(max_workers=min(max_workers, len(items)),
                            thread_name_prefix="landmine") as ex:
        # executor.map yields results in submission order and re-raises the
        # first task exception when that result is consumed by list().
        return list(ex.map(fn, items))
