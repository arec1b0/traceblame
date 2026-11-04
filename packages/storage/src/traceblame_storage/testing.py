"""Test support: where the servers of the integration tests listen."""

from dataclasses import dataclass

from traceblame_storage.client import ClickHouseClient, parse_url


@dataclass(frozen=True)
class ClickHouseServer:
    host: str
    http_port: int
    native_port: int
    user: str
    password: str

    def url(self, database: str) -> str:
        return f"http://{self.user}:{self.password}@{self.host}:{self.http_port}/{database}"

    def client(self, database: str) -> ClickHouseClient:
        return ClickHouseClient(parse_url(self.url(database)))


@dataclass(frozen=True)
class PostgresServer:
    host: str
    port: int
    user: str
    password: str

    def url(self, database: str) -> str:
        return f"postgresql://{self.user}:{self.password}@{self.host}:{self.port}/{database}"


@dataclass(frozen=True)
class NatsServer:
    host: str
    port: int
    monitor_port: int

    @property
    def url(self) -> str:
        return f"nats://{self.host}:{self.port}"
