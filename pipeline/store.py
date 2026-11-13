"""Database access and the review queue.

`PostgresStore` runs the same statements the original scripts ran, with one protective change:
an update never replaces a stored value with null. Tests use `MemoryStore`, which has the same methods.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime

STAGE_SCRAPPED_MAX = 50


def clean(record: dict) -> dict:
    """Empty strings become null; the stage label is cut to the column width."""
    out = {key: (None if value == "" else value) for key, value in record.items()}
    if out.get("stage_scrapped"):
        out["stage_scrapped"] = str(out["stage_scrapped"])[:STAGE_SCRAPPED_MAX]
    return out


def without_nulls(updates: dict) -> dict:
    """Keys whose new value is null are dropped, so an update can add or correct but never erase."""
    return {key: value for key, value in updates.items() if value is not None}


class PostgresStore:
    def __init__(self, connection):
        self.connection = connection

    @classmethod
    def connect(cls, settings) -> PostgresStore:
        import psycopg2

        return cls(psycopg2.connect(user=settings.db_user, password=settings.db_password, host=settings.db_host,
                                    port=settings.db_port, dbname=settings.db_name))

    def project_exists(self, table: str, project_name: str) -> bool:
        from psycopg2 import sql

        query = sql.SQL("SELECT 1 FROM {} WHERE project_name = %s").format(sql.Identifier(table))
        with self.connection.cursor() as cursor:
            cursor.execute(query, (project_name,))
            return cursor.fetchone() is not None

    def fetch_record(self, table: str, project_name: str) -> dict | None:
        from psycopg2 import extras, sql

        query = sql.SQL("SELECT * FROM {} WHERE project_name = %s").format(sql.Identifier(table))
        with self.connection.cursor(cursor_factory=extras.DictCursor) as cursor:
            cursor.execute(query, (project_name,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def insert(self, table: str, record: dict) -> None:
        from psycopg2 import sql

        record = clean(record)
        query = sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
            sql.Identifier(table),
            sql.SQL(", ").join(map(sql.Identifier, record.keys())),
            sql.SQL(", ").join(sql.SQL("%s") for _ in record),
        )
        self._write(query, list(record.values()))

    def update(self, table: str, project_name: str, updates: dict) -> None:
        from psycopg2 import sql

        updates = without_nulls(clean(updates))
        if not updates:
            return
        assignments = [sql.SQL("{} = %s").format(sql.Identifier(key)) for key in updates]
        query = sql.SQL("UPDATE {} SET {} WHERE project_name = %s").format(sql.Identifier(table), sql.SQL(", ").join(assignments))
        self._write(query, list(updates.values()) + [project_name])

    def _write(self, query, values) -> None:
        try:
            with self.connection.cursor() as cursor:
                cursor.execute(query, values)
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

    def close(self) -> None:
        self.connection.close()


class MemoryStore:
    """In-memory stand-in with the same interface, for tests and dry runs."""

    def __init__(self, tables: dict[str, list[dict]] | None = None):
        self.tables = tables or {}
        self.writes: list[tuple] = []

    def _find(self, table: str, project_name: str) -> dict | None:
        return next((row for row in self.tables.get(table, []) if row.get("project_name") == project_name), None)

    def project_exists(self, table: str, project_name: str) -> bool:
        return self._find(table, project_name) is not None

    def fetch_record(self, table: str, project_name: str) -> dict | None:
        row = self._find(table, project_name)
        return dict(row) if row else None

    def insert(self, table: str, record: dict) -> None:
        record = clean(record)
        self.tables.setdefault(table, []).append(record)
        self.writes.append(("insert", table, record))

    def update(self, table: str, project_name: str, updates: dict) -> None:
        updates = without_nulls(clean(updates))
        if updates:
            self._find(table, project_name).update(updates)
            self.writes.append(("update", table, project_name, updates))

    def close(self) -> None:
        pass


class ReviewQueue:
    """Values the checks could not support are appended here, one JSON object per line, for a person to look at."""

    def __init__(self, path: str):
        self.path = path

    def add(self, article: dict, table: str | None, report: dict, withheld: dict) -> None:
        entry = {
            "queued_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "article_id": article.get("id"), "project_name": article.get("project_name"), "link": article.get("link"),
            "table": table, "withheld_values": withheld, "report": report,
        }
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, default=str) + "\n")
