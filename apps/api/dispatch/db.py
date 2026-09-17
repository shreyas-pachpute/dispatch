"""One small database layer for two dialects.

Production is Postgres (Neon, via DATABASE_URL). Local development without a database falls back to
SQLite. Queries are written in the Postgres flavour (%s placeholders, ON CONFLICT upserts, RETURNING);
the SQLite path translates placeholders. DDL uses a {SERIAL} token because that is the one construct
the two dialects spell differently.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from pathlib import Path
from typing import Any


class DB:
    def __init__(self, url: str | None, sqlite_path: str | Path, schema: str):
        self.pg = bool(url and url.startswith(("postgres://", "postgresql://")))
        self.schema = schema
        self.url = url
        if self.pg:
            from psycopg.rows import dict_row
            from psycopg_pool import ConnectionPool

            # search_path is set per connection rather than as a startup option: connection poolers such as
            # Neon's reject startup options, and a direct connection keeps the SET for its lifetime
            def configure(conn: Any) -> None:
                conn.execute(f"SET search_path TO {schema}, public")

            self.pool = ConnectionPool(
                url,  # type: ignore[arg-type]
                min_size=0,
                max_size=4,
                open=True,
                configure=configure,
                kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
            )
            with self.pool.connection() as c:
                c.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
        else:
            Path(sqlite_path).parent.mkdir(parents=True, exist_ok=True)
            self._lock = threading.RLock()
            self.conn = sqlite3.connect(str(sqlite_path), check_same_thread=False)
            self.conn.row_factory = sqlite3.Row

    @property
    def label(self) -> str:
        return f"Postgres · schema {self.schema}" if self.pg else "SQLite (local)"

    # ---- DDL
    def script(self, ddl: str) -> None:
        serial = "SERIAL PRIMARY KEY" if self.pg else "INTEGER PRIMARY KEY AUTOINCREMENT"
        ddl = ddl.replace("{SERIAL}", serial)
        if self.pg:
            with self.pool.connection() as c:
                for stmt in [s.strip() for s in ddl.split(";") if s.strip()]:
                    c.execute(stmt)
        else:
            with self._lock:
                self.conn.executescript(ddl)
                self.conn.commit()

    # ---- queries
    def q(self, sql: str, args: tuple = ()) -> list[dict[str, Any]]:
        if self.pg:
            with self.pool.connection() as c:
                return [dict(r) for r in c.execute(sql, args or None).fetchall()]
        with self._lock:
            return [dict(r) for r in self.conn.execute(sql.replace("%s", "?"), args).fetchall()]

    def one(self, sql: str, args: tuple = ()) -> dict[str, Any] | None:
        rows = self.q(sql, args)
        return rows[0] if rows else None

    def x(self, sql: str, args: tuple = ()) -> None:
        if self.pg:
            with self.pool.connection() as c:
                c.execute(sql, args or None)
        else:
            with self._lock:
                self.conn.execute(sql.replace("%s", "?"), args)
                self.conn.commit()

    def lock_hint(self) -> str:
        """Appended to a claim query so two workers never take the same row on Postgres."""
        return " FOR UPDATE SKIP LOCKED" if self.pg else ""


def connect(default_sqlite: str | Path, schema: str) -> DB:
    # prefer the direct (unpooled) connection string: the per-connection search_path needs a real session
    url = os.environ.get("DATABASE_URL_UNPOOLED") or os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL")
    return DB(url, os.environ.get(f"{schema.upper()}_DB", default_sqlite), schema)
