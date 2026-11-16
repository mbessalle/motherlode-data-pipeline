import time

from evals.limits import run_with_limits


def _job(seconds):
    if seconds < 0:
        raise ValueError("bad input")
    time.sleep(seconds)
    return seconds * 2


def test_a_stuck_job_is_killed_at_the_limit_and_the_rest_finish():
    started = time.monotonic()
    results, failed = run_with_limits([0, 60, 0.1, -1, 0], _job, workers=3, limit=1, stall=5, every=100)
    assert results == [0, None, 0.2, None, 0]
    assert sorted(failed) == [1, 3]                     # 1 timed out, 3 raised
    assert time.monotonic() - started < 10              # nobody waited for the 60 s job
