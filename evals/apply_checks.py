"""Apply the three tiers' verdicts to the stored rows: a corrected copy where every cell says who verified it.

    python -m evals.apply_checks RUN_DIR      # reads stored.json, cells.jsonl, parties.jsonl, escalations.jsonl

Order of authority, highest first, and only with evidence code could confirm:
  opus   a verdict whose quote is in the document and whose value is in the quote or follows by a rule
  jev    accept (P >= 0.8), or a correction of a single-number field
  code   the validator's FOUND / DERIVED
A cell stays `review` when Opus is unsure, when its quote could not be confirmed, or when the row combines several
projects (critic finding F6: one row for "A and B" while the client wants one row per project with an equal
split), because then no single value is right until the row is split.

Writes RUN_DIR/corrected.json (rows with values) and RUN_DIR/verification.jsonl (one line per cell: old value,
new value, verified_by, reason), and a section of summary.md. Nothing is written to any database.
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from evals.cell_check import projects_named
from pipeline.schema import HECTARES_PER_UNIT
from pipeline.validate import to_number


def load(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def apply(run_dir: Path) -> tuple[dict, list[dict]]:
    stored = json.loads((run_dir / "stored.json").read_text())
    # keyed by deal id AND project: a deal over several projects has one row per project
    cells = {(c["row_id"], c["project"], c["field"]): c for c in load(run_dir / "cells.jsonl")}
    parties = {(p["row_id"], p["project"]): p for p in load(run_dir / "parties.jsonl")}
    opus = {}
    for r in load(run_dir / "escalations.jsonl"):
        for c in r.get("cells", []):
            opus[(r["row_id"], r["project"], c["field"])] = c
        opus[(r["row_id"], r["project"], "buyer")] = r.get("buyer") or {}
    report = {e["project_name"]: e["id"] for e in json.loads((run_dir / "run_report.json").read_text()) if e.get("project_name")}
    lines = []
    for rows in stored.values():
        for row in rows:
            row_id = report.get(row.get("project_name"))
            combined = projects_named(row.get("project_name") or "") > 1
            # every checked cell of the row, including values the validator withheld (they are not in the row)
            project = row.get("project_name")
            for field in sorted({f for (r, pr, f) in cells if r == row_id and pr == project}) + ["owner_name"]:
                old = row.get(field)
                key = (row_id, project, field)
                o, c = opus.get(key), cells.get(key)
                new, by, why = old, "unchecked", ""
                if field == "owner_name":
                    p, buyer = parties.get((row_id, project), {}), opus.get((row_id, project, "buyer"), {})
                    if o and o.get("quote_checked") is not False and buyer.get("quote_checked"):
                        new, by, why = buyer["name"] if o["verdict"] == "wrong" else old, "opus", o["reason"]
                    elif p.get("action") == "accept":
                        by, why = "jev", f"P(owner is buyer) {p['p_owner_is_buyer']:.2f}"
                    elif p:
                        by, why = "review", "buyer not settled"
                elif o and combined and o["verdict"] != "correct":
                    by, why = "review", f"row combines {projects_named(row['project_name'])} projects (F6): split it first"
                elif o and o["verdict"] in ("correct", "wrong") and o["quote_checked"]:
                    value = o["value"]
                    if field == "project_area" and value is not None:   # stored rows hold hectares
                        factor = HECTARES_PER_UNIT.get((o.get("unit") or "").strip().lower())
                        value = None if factor is None else round(value * factor, 4)
                    if o["verdict"] == "wrong" and value is None:
                        new, by, why = old, "review", f"opus: wrong, but its unit {o.get('unit')!r} is not convertible"
                    else:
                        new, by, why = (old if o["verdict"] == "correct" else value), "opus", o["reason"]
                elif o:
                    by, why = "review", f"opus {o['verdict']}" + ("" if o["quote_checked"] else ", quote not confirmed")
                elif c and c["action"] == "accept":
                    by, why = "jev" if c.get("p_correct") is not None else "code", c["source"]
                elif c and c["action"] == "correct":
                    new, by, why = c["proposal"], "jev", f"single-number field, P {c['p_proposal']:.2f}"
                elif c:
                    by, why = "review", c["source"]
                row[field] = new
                lines.append({"row_id": row_id, "project": row.get("project_name"), "field": field, "old": old, "new": new,
                              "changed": to_number(new) != to_number(old) if field != "owner_name" else new != old,
                              "verified_by": by, "reason": why})
    return stored, lines


def summary(lines: list[dict]) -> str:
    by = Counter(line["verified_by"] for line in lines)
    changed = [line for line in lines if line["changed"]]
    out = ["", "## Corrected rows (who verified each cell)\n",
           f"- Cells: {len(lines)}; verified by: {dict(by)}; changed: {len(changed)}", "",
           "| Row | Field | Old | New | By | Reason |", "|---|---|---|---|---|---|"]
    out += [f"| {x['row_id']} | `{x['field']}` | {x['old']} | {x['new']} | {x['verified_by']} | {x['reason'][:90]} |" for x in changed]
    out += ["", "Still in review:", ""]
    out += [f"- {x['row_id']} `{x['field']}` = {x['old']}: {x['reason'][:110]}" for x in lines if x["verified_by"] == "review"]
    return "\n".join(out) + "\n"


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("run_dir", type=Path)
    args = parser.parse_args(argv)
    corrected, lines = apply(args.run_dir)
    (args.run_dir / "corrected.json").write_text(json.dumps(corrected, indent=1, default=str))
    (args.run_dir / "verification.jsonl").write_text("".join(json.dumps(x, default=str) + "\n" for x in lines))
    text = summary(lines)
    path = args.run_dir / "summary.md"
    path.write_text(path.read_text().split("\n## Corrected rows")[0].rstrip("\n") + "\n" + text)
    print(text)


if __name__ == "__main__":
    main()
