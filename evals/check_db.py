"""Check the rows stored in the database against their source documents, and correct them.

    python -m evals.check_db check EXPORT RUN_DIR       # code and Jev on every row that has a document
    python -m evals.escalate run RUN_DIR                # Opus, only for the rows the first two could not settle
    python -m evals.check_db corrections EXPORT RUN_DIR # the correction file: old, new, evidence, who decided
    python -m evals.check_db apply EXPORT RUN_DIR       # backup tables, then all corrections in one transaction

EXPORT is a JSON export of the tables ({table: [rows]}, read-only). A row's document is its PDF from the client's
project folders (MOTHERLODE_CASES / MOTHERLODE_PDFS, matched by row id) or, for a public news link, the page itself
read with the pipeline's reader. Rows with neither are listed as unchecked, never guessed.

A value is corrected only on evidence code confirmed: an Opus verdict whose quote is in the document and whose
value is in the quote or follows by a rule, or an exact code result (hectares). Everything else stays as stored
and is listed for review. `apply` first copies every table it changes to <table>_backup_<time>, then writes all
corrections in one transaction, so a failure changes nothing and the backup restores any value.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import threading
from pathlib import Path

from evals import end_to_end
from evals.cell_check import FIELDS, check_cell, check_parties
from evals.documents import document_text
from evals.limits import run_with_limits
from pipeline import config
from pipeline.pdf import pdf_text
from pipeline.schema import HECTARES_PER_UNIT
from pipeline.search import read_page
from pipeline.validate import to_number, validate

CHECKED = [f for f in FIELDS]


def key_of(table: str, row: dict) -> str:
    return f"{table}:{row['id']}"


LIMIT = 30          # seconds: a row or a document lookup still running after this is killed and listed
_PDF_LOCK = threading.Lock()


def _locked_pdf_text(content, ocr=None):
    with _PDF_LOCK:                                    # pdfium (OCR path) is not thread-safe
        return pdf_text(content, ocr)


def find_release(row: dict, searcher) -> str | None:
    """The original announcement for a row whose link is only MiningHub's summary: the pipeline's own search on the
    headline and company, accepted only by its identity test (company named, headline names and numbers, date)."""
    article = {"id": row.get("id"), "title": row.get("news_title") or "", "company": (row.get("owner_name") or "").lower(),
               "date": f"{str(row['news_date'])[:10]}T00:00:00Z" if row.get("news_date") else ""}
    if not article["title"] or not article["company"]:
        return None
    try:
        source, _ = searcher.find_announcement(article)
    except Exception:
        return None
    return source.text if source else None


def documents(export: dict, run_dir: Path, workers: int = 8) -> dict[str, str]:
    """{row key: path of its document}: the client's PDF by row id; else the public news link read as text; else, for
    a MiningHub link, the original release found by search. Found texts are saved, so a re-run searches nothing."""
    from pipeline import search as search_module

    cases = {c["row_id"]: c for c in end_to_end.load_cases(end_to_end.DEFAULT_CASES.resolve())}
    pdfs = end_to_end.DEFAULT_PDFS.resolve()
    folder = run_dir / "docs"
    folder.mkdir(parents=True, exist_ok=True)
    search_module.pdf_text = _locked_pdf_text
    serper = os.getenv("SERPER_API_KEY")

    def one(item):
        table, row = item
        key, link = key_of(table, row), row.get("news_link") or ""
        if table == "oceania" and row["id"] in cases and cases[row["id"]].get("file"):
            return key, str(pdfs / cases[row["id"]]["project"] / cases[row["id"]]["file"])
        path = folder / f"{key.replace(':', '_')}.txt"
        missing = folder / f"{key.replace(':', '_')}.none"
        if path.exists():
            return key, str(path)
        if missing.exists():
            return key, None
        if link and "mininghub" not in link:
            text, _ = read_page(link)
        elif serper:
            text = find_release(row, search_module.Searcher(serper, max_candidates=5))
        else:
            return key, None
        if not text:
            missing.write_text("")
            return key, None
        path.write_text(text)
        return key, str(path)

    items = [(t, r) for t, rs in export.items() for r in rs]
    found, _ = run_with_limits(items, one, workers=workers, limit=LIMIT, stall=LIMIT, label="documents looked up")
    return {k: v for k, v in (f for f in found if f) if v}


def check(export_path: Path, run_dir: Path, limit: int | None = None, workers: int = 8) -> None:
    export = json.loads(export_path.read_text())
    run_dir.mkdir(parents=True, exist_ok=True)
    docs = documents(export, run_dir)
    rows = [(t, r) for t, rs in export.items() for r in rs if key_of(t, r) in docs][:limit]
    texts = {key_of(t, r): document_text(docs[key_of(t, r)]) for t, r in rows}   # sequential: pdfium is not thread-safe

    def one(item):
        table, row = item
        key, text = key_of(table, row), texts[key_of(table, row)]
        project = row.get("project_name") or ""
        report = validate(row, text)
        cells, done = [], {}
        for field, chk in sorted(report.fields.items(), key=lambda kv: {"aggregate_deal_value": 2, "shares_value": 1}.get(kv[0], 0)):
            value = to_number(row.get(field))
            if field not in FIELDS or not value:
                continue
            unit = row.get("project_area_unit") if field == "project_area" else row.get(end_to_end.CURRENCIES.get(field, ""))
            cell = check_cell(text, project, field, value, unit, {"status": chk.status, "evidence": chk.evidence},
                              row.get("owner_ticker"), str(row.get("news_date") or "")[:10] or None, row, done)
            done[field] = cell
            cells.append({"row_id": key, "project": project, "project_count": 1, **cell})
        party = {"row_id": key, "project": project, **check_parties(text, row, project, None)}
        entry = {"id": key, "project_name": project, "source_url": docs[key], "validation": report.as_dict()}
        return entry, cells, party

    # Each row in its own process, killed after LIMIT seconds; the run stops with STALLED if nothing finishes.
    out, failed = run_with_limits(rows, one, workers=workers, limit=LIMIT, stall=LIMIT, label="rows checked")
    results = [r for r in out if r is not None]
    timed_out = [key_of(*rows[i]) for i in failed]
    (run_dir / "timed_out.json").write_text(json.dumps(timed_out))
    write = lambda name, lines: (run_dir / name).write_text("".join(json.dumps(x, default=str) + "\n" for x in lines))  # noqa: E731
    (run_dir / "run_report.json").write_text(json.dumps([r[0] for r in results], default=str))
    write("cells.jsonl", [c for r in results for c in r[1]])
    write("parties.jsonl", [r[2] for r in results])
    unchecked = [key_of(t, r) for t, rs in export.items() for r in rs if key_of(t, r) not in docs]
    (run_dir / "unchecked.json").write_text(json.dumps(unchecked))
    cells = [c for r in results for c in r[1]]
    print(f"rows checked: {len(results)}; timed out: {len(timed_out)}; without a document: {len(unchecked)}; cells: {len(cells)}; "
          f"accepted by code and Jev: {sum(c['action'] == 'accept' for c in cells)}; "
          f"to Opus: {len({c['row_id'] for c in cells if c['action'] != 'accept'})} rows")


def corrections(export_path: Path, run_dir: Path) -> list[dict]:
    """Only evidence code confirmed changes a value: an Opus verdict with a confirmed quote."""
    export = json.loads(export_path.read_text())
    rows = {key_of(t, r): (t, r) for t, rs in export.items() for r in rs}
    out = []
    for line in (run_dir / "escalations.jsonl").read_text().splitlines():
        esc = json.loads(line)
        table, row = rows[esc["row_id"]]
        for cell in esc.get("cells", []):
            field, old = cell["field"], row.get(cell["field"])
            if cell["verdict"] != "wrong" or not cell.get("quote_checked") or field not in row:
                continue
            new = cell["value"]
            if field == "project_area" and new is not None:            # the tables hold hectares
                factor = HECTARES_PER_UNIT.get((cell.get("unit") or "").strip().lower())
                if factor is None:
                    continue
                new = round(new * factor, 4)
            if to_number(new) == to_number(old):
                continue
            out.append({"table": table, "id": row["id"], "project": row.get("project_name"), "field": field, "old": old,
                        "new": new, "evidence": cell["quote"], "reason": cell["reason"], "decided_by": "opus, quote confirmed"})
        buyer = esc.get("buyer") or {}
        owner_cell = next((c for c in esc.get("cells", []) if c["field"] == "owner_name"), None)
        if buyer.get("quote_checked") and buyer.get("name") and owner_cell and owner_cell["verdict"] == "wrong":
            out.append({"table": table, "id": row["id"], "project": row.get("project_name"), "field": "owner_name",
                        "old": row.get("owner_name"), "new": buyer["name"], "evidence": buyer["quote"],
                        "reason": buyer.get("reason"), "decided_by": "opus, quote confirmed"})
    (run_dir / "corrections.jsonl").write_text("".join(json.dumps(c, default=str) + "\n" for c in out))
    return out


def apply(export_path: Path, run_dir: Path) -> None:
    import psycopg2
    from psycopg2 import sql

    fixes = [json.loads(line) for line in (run_dir / "corrections.jsonl").read_text().splitlines()]
    if not fixes:
        print("nothing to apply")
        return
    s = config.load()
    conn = psycopg2.connect(user=s.db_user, password=s.db_password, host=s.db_host, port=s.db_port, dbname=s.db_name)
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%d%H%M")
    try:
        with conn, conn.cursor() as cur:            # one transaction: all or nothing
            for table in sorted({f["table"] for f in fixes}):
                cur.execute(sql.SQL("CREATE TABLE {} AS TABLE {}").format(
                    sql.Identifier(f"{table}_backup_{stamp}"), sql.Identifier(table)))
            for f in fixes:
                cur.execute(sql.SQL("UPDATE {} SET {} = %s WHERE id = %s AND {} IS NOT DISTINCT FROM %s").format(
                    sql.Identifier(f["table"]), sql.Identifier(f["field"]), sql.Identifier(f["field"])),
                    (f["new"], f["id"], f["old"]))
                if cur.rowcount != 1:
                    raise RuntimeError(f"{f['table']} id {f['id']} {f['field']}: the stored value changed since the export")
        print(f"applied {len(fixes)} corrections; backups: " + ", ".join(f"{t}_backup_{stamp}" for t in sorted({f['table'] for f in fixes})))
    finally:
        conn.close()


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("check", "corrections", "apply"))
    parser.add_argument("export", type=Path)
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--limit", type=int)
    args = parser.parse_args(argv)
    if args.command == "check":
        check(args.export, args.run_dir, args.limit)
    elif args.command == "corrections":
        fixes = corrections(args.export, args.run_dir)
        print(f"{len(fixes)} corrections in {args.run_dir / 'corrections.jsonl'}")
        for f in fixes:
            print(f"  {f['table']} id {f['id']} {f['project']!r} {f['field']}: {f['old']} -> {f['new']}  ({f['reason'][:80]})")
    else:
        apply(args.export, args.run_dir)


if __name__ == "__main__":
    main()
