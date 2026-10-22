#!/usr/bin/env python3
"""Parallel ARTICLE fetch: multiple NNTP connections + pipelined batches."""

from __future__ import annotations

import logging
import queue
import socket
import threading
import time
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from .nntp import NNTPClient, NNTPError, NNTPTimeout, is_missing_article, is_too_big
from .overview import OverviewRow

logger = logging.getLogger(__name__)

# Sentinel meaning a worker has finished.
_WORKER_DONE = object()


def _chunks(items: Sequence, size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def _make_client(conn_kwargs: dict) -> NNTPClient:
    return NNTPClient(**conn_kwargs)


class _Workers:
    """What the fetch shares with its workers: the abort event and their
    current clients, so that an abort can cut a read that is in progress."""

    def __init__(self) -> None:
        self.abort = threading.Event()
        self._lock = threading.Lock()
        self._clients: Dict[int, NNTPClient] = {}

    def register(self, worker_id: int, client: NNTPClient) -> None:
        with self._lock:
            self._clients[worker_id] = client
        if self.abort.is_set():
            self._cut(client)

    def cut_all(self) -> None:
        with self._lock:
            clients = list(self._clients.values())
        for client in clients:
            self._cut(client)

    @staticmethod
    def _cut(client: NNTPClient) -> None:
        conn = client.conn
        if conn is not None:
            try:
                conn.shutdown(socket.SHUT_RDWR)  # wakes a blocked recv
            except OSError:
                pass


def _put(out_queue: queue.Queue, item, abort: threading.Event) -> bool:
    """Put item, waiting while the queue is full; False once aborted."""
    while True:
        try:
            out_queue.put(item, timeout=0.2)
            return True
        except queue.Full:
            if abort.is_set():
                return False


def _worker(
    worker_id: int,
    rows: Sequence[OverviewRow],
    group: str,
    conn_kwargs: dict,
    pipeline_depth: int,
    out_queue: queue.Queue,
    stop_event: threading.Event,
    shared: Optional[_Workers] = None,
) -> None:
    """Fetch assigned rows; push (row, content, resp) to out_queue.

    Stops at the next batch when stop_event is set, and at once when the
    fetch aborts (shared.abort): then nothing more is put and the
    connection is closed without waiting for the server.
    """
    shared = shared or _Workers()
    abort = shared.abort
    client: Optional[NNTPClient] = None
    emitted = 0

    def stopped() -> bool:
        return stop_event.is_set() or abort.is_set()

    def new_client() -> NNTPClient:
        c = _make_client(conn_kwargs)
        shared.register(worker_id, c)
        c.connect()
        c.group(group)
        return c

    try:
        client = new_client()

        for batch in _chunks(list(rows), max(1, pipeline_depth)):
            if stopped():
                break
            ids = [r.article_id for r in batch]
            retries = 3
            while retries > 0:
                if stopped():
                    break
                try:
                    results = client.articles_pipelined(ids)
                    for row, (_spec, content, resp) in zip(batch, results):
                        if not _put(out_queue, (row, content, resp), abort):
                            return
                        emitted += 1
                    break
                except (NNTPTimeout, NNTPError, OSError) as e:
                    if abort.is_set():
                        return
                    retries -= 1
                    logger.warning(
                        "worker %s pipeline failed (%s); retries left %s",
                        worker_id,
                        e,
                        retries,
                    )
                    if retries == 0:
                        for row in batch:
                            if not _put(out_queue, (row, b"", f"error: {e}"), abort):
                                return
                            emitted += 1
                    # The stream may be out of step (a late or half reply):
                    # never read this connection again, not even for QUIT.
                    client.close()
                    client = new_client()
                    if retries == 0:
                        break
    except Exception as e:
        if not abort.is_set():
            logger.error("worker %s fatal: %s", worker_id, e)
            for row in rows[emitted:]:
                if not _put(out_queue, (row, b"", f"error: {e}"), abort):
                    break
    finally:
        if client is not None:
            try:
                if abort.is_set():
                    client.close()
                else:
                    client.quit()
            except Exception:
                try:
                    client.close()
                except Exception:
                    pass
        _put(out_queue, _WORKER_DONE, abort)


def fetch_articles_parallel(
    rows: Sequence[OverviewRow],
    group: str,
    conn_kwargs: dict,
    connections: int,
    pipeline_depth: int,
    on_article: Callable[[OverviewRow, bytes, str], None],
    stop_event: Optional[threading.Event] = None,
    progress_every: int = 100,
) -> Tuple[int, int, int, float]:
    """Fetch articles using ``connections`` clients and pipelined batches.

    ``on_article(row, content, resp)`` is called from the main thread for each
    result (thread-safe writer assumed). Returns
    ``(fetched_ok, missing, errors, elapsed_seconds)``.
    """
    if not rows:
        return 0, 0, 0, 0.0

    stop_event = stop_event or threading.Event()
    n_conn = max(1, min(connections, len(rows)))
    depth = max(1, pipeline_depth)

    # Round-robin split so each worker gets a similar mix of article ages.
    shards: List[List[OverviewRow]] = [[] for _ in range(n_conn)]
    for i, row in enumerate(rows):
        shards[i % n_conn].append(row)
    shards = [s for s in shards if s]
    n_conn = len(shards)

    logger.info(
        "Fetching %s articles with %s connection(s), pipeline depth %s",
        len(rows),
        n_conn,
        depth,
    )

    out_queue: queue.Queue = queue.Queue(maxsize=max(256, n_conn * depth * 2))
    shared = _Workers()
    threads = []
    for wid, shard in enumerate(shards):
        t = threading.Thread(
            target=_worker,
            name=f"nntp-fetch-{wid}",
            args=(wid, shard, group, conn_kwargs, depth, out_queue, stop_event, shared),
            daemon=True,
        )
        threads.append(t)
        t.start()

    fetched_ok = 0
    missing = 0
    too_big = 0
    errors = 0
    done_workers = 0
    processed = 0
    t0 = time.monotonic()
    last_log = t0
    bytes_ok = 0

    try:
        while done_workers < n_conn:
            try:
                item = out_queue.get(timeout=1.0)
            except queue.Empty:
                if stop_event.is_set():
                    break
                # Check for dead threads without sentinel (shouldn't happen).
                if not any(t.is_alive() for t in threads) and out_queue.empty():
                    break
                continue

            if item is _WORKER_DONE:
                done_workers += 1
                continue

            row, content, resp = item
            on_article(row, content, resp)
            processed += 1
            if content:
                fetched_ok += 1
                bytes_ok += len(content)
            elif is_missing_article(resp):
                missing += 1
            elif is_too_big(resp):
                too_big += 1
            else:  # "error: ..." from the worker, or a status such as 480/502
                errors += 1

            now = time.monotonic()
            if processed % progress_every == 0 or (now - last_log) >= 5.0:
                elapsed = max(now - t0, 1e-6)
                arts_per_s = processed / elapsed
                kib_per_s = (bytes_ok / 1024.0) / elapsed
                logger.info(
                    "progress: %s/%s articles (%.1f art/s, %.1f KiB/s, ok=%s missing=%s too-big=%s err=%s)",
                    processed,
                    len(rows),
                    arts_per_s,
                    kib_per_s,
                    fetched_ok,
                    missing,
                    too_big,
                    errors,
                )
                last_log = now
    finally:
        if done_workers < n_conn:
            # stopped early (an error in on_article, a stop): the workers
            # must not stay blocked on the full queue or in a read, holding
            # their connections
            shared.abort.set()
            shared.cut_all()
        for t in threads:
            t.join(timeout=10)

    elapsed = max(time.monotonic() - t0, 1e-6)
    return fetched_ok, missing, errors, elapsed
