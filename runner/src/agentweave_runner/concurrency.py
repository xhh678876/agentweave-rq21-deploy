"""Concurrency helpers — thin wrappers around ThreadPoolExecutor.

Why threads, not processes:
    - The LangGraph harness is IO-bound on LLM calls and tool-mock state.
    - Threads share library update serialization (a single lock) cheaply.
    - We do NOT need GIL bypass; the heavy work happens in the API server.

Library write serialization is enforced via ``LibraryLock`` (re-entrant lock).
"""

from __future__ import annotations

import threading
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from typing import Any, Callable, Iterable, Iterator

DEFAULT_CONCURRENCY = 32


class LibraryLock:
    """Re-entrant lock around library reads + writes.

    Used by the runner to serialize ``update_library`` while letting
    ``inject_library`` reads happen concurrently. We use a single RLock for
    simplicity; if the read path becomes a bottleneck we can swap in a
    Reader-Writer lock without changing the call sites.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()

    @contextmanager
    def write(self) -> Iterator[None]:
        with self._lock:
            yield

    @contextmanager
    def read(self) -> Iterator[None]:
        with self._lock:
            yield


def run_in_parallel(
    items: Iterable[Any],
    worker: Callable[[Any], Any],
    *,
    concurrency: int = DEFAULT_CONCURRENCY,
) -> list[tuple[Any, Any, Exception | None]]:
    """Run ``worker(item)`` concurrently over ``items``.

    Returns a list of (item, result, error) tuples in the original input
    order. Exceptions from individual workers are captured, not re-raised, so
    one bad task does not abort the whole run.
    """
    items_list = list(items)
    if not items_list:
        return []
    out: list[tuple[Any, Any, Exception | None]] = [
        (item, None, None) for item in items_list
    ]
    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        futures: dict[Future, int] = {}
        for index, item in enumerate(items_list):
            fut = pool.submit(worker, item)
            futures[fut] = index
        for fut in futures:
            idx = futures[fut]
            try:
                result = fut.result()
                out[idx] = (items_list[idx], result, None)
            except Exception as exc:  # noqa: BLE001
                out[idx] = (items_list[idx], None, exc)
    return out
