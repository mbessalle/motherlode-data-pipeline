"""Applying the tiers' verdicts: per project, in hectares, and only with evidence code confirmed. Offline."""
import json

from evals import apply_checks


def write(path, lines):
    path.write_text("".join(json.dumps(x) + "\n" for x in lines))


def test_each_project_keeps_its_own_verdict_and_areas_are_stored_in_hectares(tmp_path):
    (tmp_path / "stored.json").write_text(json.dumps({"oceania": [
        {"project_name": "Bonnie Vale", "project_area": 9000.0, "owner_name": "Forrestania"},
        {"project_name": "Breakaway Dam", "project_area": 10500.0, "owner_name": "Forrestania"}]}))
    (tmp_path / "run_report.json").write_text(json.dumps([{"id": 309, "project_name": "Bonnie Vale"},
                                                          {"id": 309, "project_name": "Breakaway Dam"}]))
    write(tmp_path / "cells.jsonl", [
        {"row_id": 309, "project": "Bonnie Vale", "field": "project_area", "action": "review", "source": "stated"},
        {"row_id": 309, "project": "Breakaway Dam", "field": "project_area", "action": "accept", "source": "stated",
         "p_correct": 0.9}])
    write(tmp_path / "parties.jsonl", [{"row_id": 309, "project": "Bonnie Vale", "action": "accept", "p_owner_is_buyer": 0.9},
                                       {"row_id": 309, "project": "Breakaway Dam", "action": "accept", "p_owner_is_buyer": 0.9}])
    write(tmp_path / "escalations.jsonl", [{"row_id": 309, "project": "Bonnie Vale", "buyer": {"name": "Forrestania",
                                            "quote_checked": True}, "cells": [
        {"field": "project_area", "verdict": "wrong", "value": 95, "unit": "km2", "quote_checked": True,
         "reason": "Bonnie Vale covers about 95 km2"}]}])
    rows, lines = apply_checks.apply(tmp_path)
    bonnie, breakaway = rows["oceania"]
    assert bonnie["project_area"] == 9500.0                      # 95 km2, stored in hectares
    assert breakaway["project_area"] == 10500.0                  # the other project's verdict did not leak
    assert {(x["project"], x["verified_by"]) for x in lines if x["field"] == "project_area"} == {
        ("Bonnie Vale", "opus"), ("Breakaway Dam", "jev")}
