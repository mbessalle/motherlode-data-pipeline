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


def test_a_failed_opus_call_skips_its_deal_and_the_run_goes_on(tmp_path, monkeypatch):
    from evals import escalate
    monkeypatch.setattr(escalate, "flagged", lambda run_dir: {("a", 1): {"cells": []}, ("b", 1): {"cells": []}})
    monkeypatch.setattr(escalate, "document", lambda run_dir, key: ("P", "text"))
    monkeypatch.setattr(escalate, "ask_model", lambda *a: (_ for _ in ()).throw(RuntimeError("limit")))
    assert escalate.run(tmp_path) == []
    assert len(__import__("json").loads((tmp_path / "escalate_failed.json").read_text())) == 2


def test_parallel_opus_calls_are_capped_by_free_memory():
    from evals.escalate import safe_parallel
    assert safe_parallel(26, available_mb=11000) == 8      # the 14 GB laptop that froze at 26
    assert safe_parallel(4, available_mb=11000) == 4
    assert safe_parallel(26, available_mb=300) == 1


def test_blanking_and_exchange_rate_corrections_are_held_for_the_client():
    from evals.check_db import hold_reason
    assert hold_reason({"field": "shares_value", "old": 70000, "new": None}) == "blanks a value"
    assert hold_reason({"field": "cash_payments_value", "old": 301500, "new": 450000}) == "differs only by an exchange rate"
    assert hold_reason({"field": "nsr_percent", "old": 5, "new": 0.5}) is None
    assert hold_reason({"field": "owner_name", "old": "Eclipse", "new": "Boss Energy"}) is None
