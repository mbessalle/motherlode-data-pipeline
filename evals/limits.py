"""Run many slow, independent jobs with hard limits, so one stuck job can never stop a run.

Each job runs in its own forked process. A job still running after `limit` seconds is killed and reported as timed
out; its slot is reused at once. A watchdog stops the whole run with STALLED if nothing completes for `stall`
seconds. Progress is printed every `every` jobs.
"""
from __future__ import annotations

import multiprocessing
import queue as queue_module
import time
from collections.abc import Callable


class Stalled(RuntimeError):
    pass


def run_with_limits(items: list, fn: Callable, workers: int = 8, limit: float = 30, stall: float = 30,
                    every: int = 50, label: str = "jobs") -> tuple[list, list[int]]:
    """(results in item order, None for a timed-out or failed job; indexes of the jobs that timed out or failed)."""
    ctx = multiprocessing.get_context("fork")
    results_queue = ctx.Queue()
    results: list = [None] * len(items)
    failed: list[int] = []
    running: dict[int, tuple] = {}            # index -> (process, start time)
    next_index, done, last_progress = 0, 0, time.monotonic()

    def worker(index, item):
        try:
            results_queue.put((index, True, fn(item)))
        except Exception as error:            # a failing job is reported; it does not stop the run
            results_queue.put((index, False, repr(error)))

    def finished_one():
        nonlocal done, last_progress
        done += 1
        last_progress = time.monotonic()
        if done % every == 0:
            print(f"{label}: {done} of {len(items)} done; timed out or failed {len(failed)}", flush=True)

    while done < len(items):
        while len(running) < workers and next_index < len(items):
            process = ctx.Process(target=worker, args=(next_index, items[next_index]), daemon=True)
            process.start()
            running[next_index] = (process, time.monotonic())
            next_index += 1
        try:
            index, ok, value = results_queue.get(timeout=0.5)
        except queue_module.Empty:
            pass
        else:
            if index in running:              # not already killed for the time limit
                process, _ = running.pop(index)
                process.join(5)
                results[index] = value if ok else None
                if not ok:
                    failed.append(index)
                finished_one()
        now = time.monotonic()
        for index, (process, started) in list(running.items()):
            if now - started > limit:          # hard limit: kill the stuck job and free its slot
                process.kill()
                process.join(5)
                running.pop(index)
                failed.append(index)
                finished_one()
        if time.monotonic() - last_progress > stall:
            for process, _ in running.values():
                process.kill()
            raise Stalled(f"STALLED: no {label} finished for {stall:.0f} s ({done} of {len(items)} done)")
    print(f"{label}: {done} of {len(items)} done; timed out or failed {len(failed)}", flush=True)
    return results, failed
