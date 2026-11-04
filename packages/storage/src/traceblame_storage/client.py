"""Connections for the runners: clickhouse-connect over HTTP(S), psycopg for Postgres."""

from collections.abc import Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any, LiteralString, cast
from urllib.parse import unquote, urlsplit

import clickhouse_connect
import psycopg


@dataclass(frozen=True)
class Target:
    host: str
    port: int
    username: str
    password: str
    database: str
    secure: bool


def parse_url(url: str) -> Target:
    """http(s)://[user[:password]@]host[:port][/database]; the database defaults to default."""
    u = urlsplit(url)
    path = u.path.strip("/")
    if u.scheme not in ("http", "https") or not u.hostname or "/" in path:
        raise ValueError(f"url {url!r}: expected http(s)://[user[:pass]@]host[:port][/database]")
    secure = u.scheme == "https"
    return Target(
        host=u.hostname,
        port=u.port or (8443 if secure else 8123),
        username=unquote(u.username or "default"),
        password=unquote(u.password or ""),
        database=path or "default",
        secure=secure,
    )


class ClickHouseClient:
    """Implements traceblame_storage.runner.Client; creates the database if it is missing."""

    def __init__(self, target: Target) -> None:
        def connect(database: str) -> Any:
            return clickhouse_connect.get_client(  # pyright: ignore[reportUnknownMemberType]
                host=target.host,
                port=target.port,
                username=target.username,
                password=target.password,
                database=database,
                secure=target.secure,
            )

        if target.database != "default":
            admin = connect("default")
            admin.command(f"CREATE DATABASE IF NOT EXISTS `{target.database}`")
            admin.close()
        self._client: Any = connect(target.database)

    def command(self, sql: str, parameters: Mapping[str, Any] | None = None) -> object:
        return self._client.command(sql, parameters=dict(parameters or {}))

    def query_rows(
        self, sql: str, parameters: Mapping[str, Any] | None = None
    ) -> list[tuple[Any, ...]]:
        result = self._client.query(sql, parameters=dict(parameters or {}))
        rows: list[tuple[Any, ...]] = [tuple(r) for r in result.result_rows]
        return rows

    def insert(
        self, table: str, rows: Sequence[Sequence[Any]], column_names: Sequence[str]
    ) -> object:
        return self._client.insert(table, [list(r) for r in rows], column_names=list(column_names))

    def close(self) -> None:
        self._client.close()


class PostgresClient:
    """Implements traceblame_storage.pg_runner.Connection over psycopg.

    psycopg types a query as LiteralString to keep injection out. The runner executes only
    the packaged migration files and its own bookkeeping statements, which are trusted code,
    so the cast is made here once.
    """

    def __init__(self, conn: psycopg.Connection[Any]) -> None:
        self._conn = conn

    def execute(self, query: str, params: Sequence[Any] | None = None) -> psycopg.Cursor[Any]:
        return self._conn.execute(cast("LiteralString", query), params)

    def transaction(self) -> AbstractContextManager[object]:
        return self._conn.transaction()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "PostgresClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def connect_postgres(url: str) -> PostgresClient:
    """A direct, autocommit connection: the runner's advisory lock is session-scoped, so it
    must not go through a transaction-mode pooler (ADR-0007)."""
    return PostgresClient(psycopg.connect(url, autocommit=True))
