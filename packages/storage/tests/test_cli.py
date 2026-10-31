import pytest

from traceblame_storage.cli import main
from traceblame_storage.client import Target, parse_url
from traceblame_storage.migrations import packaged


def test_parse_url() -> None:
    assert parse_url("http://default:secret@localhost:8123/traceblame") == Target(
        host="localhost",
        port=8123,
        username="default",
        password="secret",  # noqa: S106 - test credentials
        database="traceblame",
        secure=False,
    )
    assert parse_url("https://ch.example:8443/db").secure
    assert parse_url("http://localhost:8123").database == "default"


@pytest.mark.parametrize("url", ["tcp://localhost:9000/db", "http:///db", "http://h/a/b"])
def test_parse_url_rejects(url: str) -> None:
    with pytest.raises(ValueError, match="url"):
        parse_url(url)


def test_check_passes_on_the_packaged_migrations(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["check"]) == 0
    assert f"{len(packaged())} migration(s) ok" in capsys.readouterr().out
