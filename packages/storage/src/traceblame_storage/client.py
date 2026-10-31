"""A ClickHouse connection for the runner: clickhouse-connect over HTTP(S)."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote, urlsplit

import clickhouse_connect


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

    def command(self, sql: str) -> object:
        return self._client.command(sql)

    def query_rows(self, sql: str) -> list[tuple[Any, ...]]:
        rows: list[tuple[Any, ...]] = [tuple(r) for r in self._client.query(sql).result_rows]
        return rows

    def insert(
        self, table: str, rows: Sequence[Sequence[Any]], column_names: Sequence[str]
    ) -> object:
        return self._client.insert(table, [list(r) for r in rows], column_names=list(column_names))

    def close(self) -> None:
        self._client.close()
