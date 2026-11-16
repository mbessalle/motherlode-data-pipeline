"""Jev against a cheap generative model (gpt-6-luna) as the judge of "is this value this field of this deal?".

    python -m evals.judge_benchmark RUN_DIR      # RUN_DIR with cells.jsonl and escalations.jsonl

Cases: the cells Jev flagged that Opus then judged correct with a quote code confirmed (true values), and for each
one a counterexample built in code: the same passage with a different number of the same kind from that passage
(a wrong value). Both judges answer every case with the same field definition, examples and counterexamples
(evals/jev_verify.FIELDS); only the model differs. Replies are cached, so a re-run is free.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path

from evals import cell_check
from evals.jev_verify import FIELDS, ask

MODEL = "openai/gpt-6-luna"
CACHE = Path(__file__).parent / ".cache" / "judge"
THRESHOLD = 0.5

SYSTEM = """You check one value in a mining deal database row against a passage of its source announcement.
Answer with a JSON object: {"correct": true or false, "probability": the probability (0 to 1) that the value is
correct, "reason": one short sentence}. Think about what the number in the passage refers to before you answer.
Arithmetic the row states as already done by code (a split across projects, a sum of payments) is correct."""


def luna(client, project: str, field: str, value: float, unit: str | None, how: str, passage: str) -> float:
    definition, yes, no = FIELDS[field]
    user = (f"Project: {project}\nField: {field}, {definition}\nValue: {value:,.10g} {unit or ''}\nHow it follows: {how}\n"
            f"It is correct when: {yes}\nIt is wrong when: {no}\n\nPassage:\n---\n{passage[:3000]}\n---")
    request = {"model": MODEL, "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}],
               "response_format": {"type": "json_object"}}
    path = CACHE / f"{hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()[:32]}.json"
    if path.exists():
        reply = json.loads(path.read_text())
    else:
        response = client.chat.completions.create(**request)
        reply = json.loads(response.choices[0].message.content)
        CACHE.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(reply))
    p = reply.get("probability")
    return float(p) if isinstance(p, int | float) else (1.0 if reply.get("correct") else 0.0)


def cases(run_dir: Path) -> list[dict]:
    confirmed = set()
    for r in map(json.loads, (run_dir / "escalations.jsonl").read_text().splitlines()):
        for c in r["cells"]:
            if c["quote_checked"] and c["verdict"] == "correct":
                confirmed.add((r["row_id"], r["project"], c["field"]))
    out = []
    for c in map(json.loads, (run_dir / "cells.jsonl").read_text().splitlines()):
        if (c["row_id"], c["project"], c["field"]) not in confirmed or not c.get("passage") or c["field"] not in FIELDS:
            continue
        how = "written in the document" if c["source"] == "stated" else c["source"]
        out.append({**c, "how": how, "label": "correct"})
        current = cell_check.hectares(c["value"], c["unit"]) if c["field"] == "project_area" else c["value"]
        others = [o for o in cell_check.candidates(c["passage"], c["field"])
                  if abs(o["value"] - current) > max(0.011, 0.01 * abs(current))]
        if others:                        # the same passage, another number of the same kind: a wrong value
            wrong = others[0]["value"] / (100 if c["field"] == "project_area" and (c["unit"] or "").lower().startswith("km") else 1)
            out.append({**c, "value": wrong, "how": "written in the document", "label": "wrong"})
    return out


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("run_dir", type=Path)
    args = parser.parse_args(argv)
    import openai

    client = openai.OpenAI(base_url="https://openrouter.ai/api/v1", api_key=os.environ["OPENROUTER_API_KEY"])
    rows = []
    for c in cases(args.run_dir):
        p_jev = ask(c["project"], c["field"], c["value"], c["unit"], c["how"], c["passage"])["p_correct"]
        p_luna = luna(client, c["project"], c["field"], c["value"], c["unit"], c["how"], c["passage"])
        rows.append({**{k: c[k] for k in ("row_id", "project", "field", "value", "label")}, "jev": p_jev, "luna": p_luna})
    (args.run_dir / "judge_benchmark.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    lines = ["| Judge | Correct values accepted | Wrong values caught | False alarms |", "|---|---|---|---|"]
    for name in ("jev", "luna"):
        good = [r for r in rows if r["label"] == "correct"]
        bad = [r for r in rows if r["label"] == "wrong"]
        accepted = sum(r[name] >= THRESHOLD for r in good)
        caught = sum(r[name] < THRESHOLD for r in bad)
        lines.append(f"| {name} | {accepted} of {len(good)} | {caught} of {len(bad)} | {len(good) - accepted} |")
    print("\n".join(lines))
    (args.run_dir / "judge_benchmark.md").write_text("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
