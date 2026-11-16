"""Render a before/after image of corrections, queried live from the database.

    python -m tools.corrections_image STAMP OUT.png [--rows table:id:field ...]

STAMP is the suffix of the backup tables `apply` made (for example 202611161043). For each chosen cell the image
shows the value in the backup table (before) and in the live table (after), read in one read-only session, so
the picture is the database's own answer, not a copy of the correction file. Needs matplotlib.
"""
from __future__ import annotations

import argparse
import datetime as dt

import psycopg2
from psycopg2 import sql

from pipeline import config

DEFAULT_ROWS = ["oceania:478:owner_name", "oceania:353:owner_name", "oceania:353:nsr_percent",
                "oceania:387:nsr_percent", "north_america:1162:project_area", "north_america:1390:project_area",
                "oceania:440:interest_acquired_percent", "north_america:1187:shares_amount",
                "africa:36:project_area", "europe:33:interest_acquired_percent"]


def fetch(stamp: str, cells: list[str]) -> list[tuple]:
    s = config.load()
    conn = psycopg2.connect(user=s.db_user, password=s.db_password, host=s.db_host, port=s.db_port, dbname=s.db_name)
    conn.set_session(readonly=True)
    out = []
    try:
        with conn.cursor() as cur:
            for cell in cells:
                table, row_id, field = cell.split(":")
                cur.execute(sql.SQL("SELECT b.project_name, b.{f}, l.{f} FROM {b} b JOIN {l} l USING (id) WHERE id = %s").format(
                    f=sql.Identifier(field), b=sql.Identifier(f"{table}_backup_{stamp}"), l=sql.Identifier(table)), (int(row_id),))
                row = cur.fetchone()
                if row:
                    out.append((table, row[0], field, row[1], row[2]))
    finally:
        conn.close()
    return out


def show(value) -> str:
    if value is None:
        return "(empty)"
    try:
        number = float(value)
        return f"{number:,.10g}"
    except (TypeError, ValueError):
        return str(value).rstrip(".")


def render(rows: list[tuple], out: str) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    header = ["Table", "Project", "Field", "Before (backup table)", "After (live table)"]
    body = [[t, p, f, show(b), show(a)] for t, p, f, b, a in rows]
    fig, ax = plt.subplots(figsize=(14, 0.36 * (len(body) + 1.5)), dpi=160)
    ax.axis("off")
    grid = ax.table(cellText=body, colLabels=header, loc="center", cellLoc="left", colLoc="left",
                    colWidths=[0.12, 0.17, 0.27, 0.22, 0.22])
    grid.auto_set_font_size(False)
    grid.set_fontsize(10.5)
    grid.scale(1, 1.55)
    for (r, c), cell in grid.get_celld().items():
        cell.set_edgecolor("#d0d7de")
        if r == 0:
            cell.set_facecolor("#24292f")
            cell.get_text().set_color("white")
            cell.get_text().set_weight("bold")
        elif c == 3:
            cell.set_facecolor("#ffebe9")
        elif c == 4:
            cell.set_facecolor("#dafbe1")
        if c == 2 and r > 0:
            cell.get_text().set_family("monospace")
    ax.set_title(f"Corrections in the production Postgres (Supabase), queried {dt.date.today():%d %b %Y}: "
                 "backup table vs live table", fontsize=11.5, loc="left", pad=6)
    fig.savefig(out, bbox_inches="tight", facecolor="white")


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("stamp")
    parser.add_argument("out")
    parser.add_argument("--rows", nargs="*", default=DEFAULT_ROWS)
    args = parser.parse_args(argv)
    rows = fetch(args.stamp, args.rows)
    render(rows, args.out)
    print(f"{len(rows)} rows -> {args.out}")


if __name__ == "__main__":
    main()
