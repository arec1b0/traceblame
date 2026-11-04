"""The Postgres migration runner and the assembler's watermark and lease statements, on a real
Postgres (ADR-0007, ADR-0003 amendment, #35)."""

import re
import threading
import time
from pathlib import Path
from typing import Any

import psycopg
import pytest

from traceblame_storage.client import connect_postgres
from traceblame_storage.migrations import POSTGRES, Migration, packaged, parse
from traceblame_storage.pg_runner import ChecksumMismatchError, migrate, status

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
STORE_SQL = ROOT / "services" / "assembler" / "internal" / "store" / "sql"


def _stmt(name: str) -> str:
    """A store statement with $n placeholders as psycopg's %(pn)s."""
    text = (STORE_SQL / name).read_text(encoding="utf-8")
    return re.sub(r"\$(\d+)", r"%(p\1)s", text)


INIT, CLAIM, ADVANCE = _stmt("init.sql"), _stmt("claim.sql"), _stmt("advance.sql")
HEARTBEAT, LIVE, PRUNE = (
    _stmt("heartbeat.sql"),
    _stmt("live_replicas.sql"),
    _stmt("prune_replicas.sql"),
)
RELEASE = _stmt("release.sql")


def _bind(*values: object) -> dict[str, object]:
    return {f"p{i}": v for i, v in enumerate(values, start=1)}


@pytest.fixture
def db(pg_database: str) -> Any:
    """A migrated database: the packaged Postgres migrations applied."""
    with connect_postgres(pg_database) as conn:
        migrate(conn, packaged(POSTGRES))
    with psycopg.connect(pg_database, autocommit=True) as c:
        yield c


def _rows(conn: psycopg.Connection[Any], count: int) -> dict[int, tuple[int, str | None, int]]:
    cur = conn.execute(
        "SELECT partition, watermark_ms, owner, epoch FROM assembler_partitions "
        "WHERE partition_count = %s ORDER BY partition",
        (count,),
    )
    return {int(p): (int(w), o, int(e)) for p, w, o, e in cur.fetchall()}


# --- the runner ------------------------------------------------------------------------------


def test_the_packaged_migrations_apply_once_and_record_their_hashes(pg_database: str) -> None:
    migrations = packaged(POSTGRES)
    with connect_postgres(pg_database) as conn:
        assert [m.version for m in migrate(conn, migrations)] == [m.version for m in migrations]
        assert migrate(conn, migrations) == [], "a second run applies nothing"
        assert {s for _, s in status(conn, migrations)} == {"applied"}
        rows = conn.execute("SELECT version, sha256 FROM schema_migrations ORDER BY version")
        assert [(int(v), s) for v, s in rows.fetchall()] == [
            (m.version, m.sha256) for m in migrations
        ]
        table = conn.execute("SELECT to_regclass('assembler_partitions')").fetchall()
        assert table[0][0] == "assembler_partitions"


def test_a_changed_applied_migration_is_refused(pg_database: str) -> None:
    migrations = packaged(POSTGRES)
    with connect_postgres(pg_database) as conn:
        migrate(conn, migrations)
        edited = [parse(migrations[0].filename, migrations[0].sql + "\n-- edited")]
        with pytest.raises(ChecksumMismatchError, match=migrations[0].filename):
            migrate(conn, edited)
        assert status(conn, edited)[0][1] == "changed"


def test_a_failing_migration_leaves_nothing_behind(pg_database: str) -> None:
    good = parse("0001_expand_a.sql", "CREATE TABLE IF NOT EXISTS a (x integer)")
    bad = parse(
        "0002_expand_b.sql",
        "CREATE TABLE IF NOT EXISTS b (x integer);\nCREATE TABLE IF NOT EXISTS c (x NOPE)",
    )
    with connect_postgres(pg_database) as conn:
        with pytest.raises(psycopg.Error):
            migrate(conn, [good, bad])
        assert conn.execute("SELECT to_regclass('b')").fetchall()[0][0] is None, "rolled back"
        assert [
            v for v, _ in conn.execute("SELECT version, sha256 FROM schema_migrations").fetchall()
        ] == [1]
        # the lock was released: another run goes through
        fixed = parse("0002_expand_b.sql", "CREATE TABLE IF NOT EXISTS b (x integer)")
        assert [m.version for m in migrate(conn, [good, fixed])] == [2]


def test_concurrent_runners_queue_on_the_advisory_lock(pg_database: str) -> None:
    migrations: list[Migration] = [
        parse(f"{i:04d}_expand_t{i}.sql", f"CREATE TABLE IF NOT EXISTS t{i} (x integer)")
        for i in range(1, 6)
    ]
    applied: list[int] = []
    errors: list[BaseException] = []
    barrier = threading.Barrier(4)

    def run() -> None:
        try:
            with connect_postgres(pg_database) as conn:
                barrier.wait(timeout=10)
                applied.append(len(migrate(conn, migrations)))
        except BaseException as e:
            errors.append(e)

    threads = [threading.Thread(target=run) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert errors == []
    assert sorted(applied) == [0, 0, 0, 5], "exactly one runner applied them"


# --- assembler_partitions: init -------------------------------------------------------------


def test_init_creates_every_partition_at_the_initial_watermark(db: Any) -> None:
    db.execute(INIT, _bind(4, 1_000))
    assert _rows(db, 4) == dict.fromkeys(range(4), (1000, None, 0))
    db.execute(INIT, _bind(4, 9_999))
    assert _rows(db, 4) == dict.fromkeys(range(4), (1000, None, 0)), "existing rows are kept"


def test_a_new_partition_count_restarts_from_the_smallest_watermark_of_the_last_count(
    db: Any,
) -> None:
    db.execute(INIT, _bind(2, 0))
    db.execute("UPDATE assembler_partitions SET watermark_ms = 5000 WHERE partition = 0")
    db.execute(
        "UPDATE assembler_partitions SET watermark_ms = 9000, updated_at = now() "
        "WHERE partition = 1"
    )
    db.execute(INIT, _bind(4, 1))
    assert _rows(db, 4) == dict.fromkeys(range(4), (5000, None, 0))
    # going back to a count that has rows resumes them: they are only ever behind, never ahead
    db.execute(INIT, _bind(2, 7))
    resumed = _rows(db, 2)
    assert (resumed[0][0], resumed[1][0]) == (5_000, 9_000)


# --- assembler_partitions: leases and the fenced advance -------------------------------------


def _claim(db: Any, owner: str, ttl: float = 30, count: int = 1, partition: int = 0) -> Any:
    return db.execute(CLAIM, _bind(count, partition, owner, ttl)).fetchone()


def _advance(db: Any, owner: str, epoch: int, new: int, old: int, ttl: float = 30) -> int:
    return int(db.execute(ADVANCE, _bind(1, 0, owner, epoch, new, ttl, old)).rowcount)


def test_a_live_lease_excludes_other_owners_and_a_renewal_keeps_the_epoch(db: Any) -> None:
    db.execute(INIT, _bind(1, 100))
    assert _claim(db, "a") == (1, 100)
    assert _claim(db, "b") is None, "a's lease is live"
    assert _claim(db, "a") == (1, 100), "a renewal keeps the epoch"


def test_an_expired_lease_is_taken_over_with_a_higher_epoch(db: Any) -> None:
    db.execute(INIT, _bind(1, 100))
    _claim(db, "a")
    db.execute("UPDATE assembler_partitions SET lease_until = now() - interval '1 second'")
    assert _claim(db, "b") == (2, 100)
    assert _claim(db, "a") is None, "b holds it now"
    db.execute("UPDATE assembler_partitions SET lease_until = now() - interval '1 second'")
    assert _claim(db, "b") == (3, 100), "an owner whose own lease had expired is fenced too"


def test_the_advance_is_a_compare_and_set_on_owner_epoch_and_watermark(db: Any) -> None:
    db.execute(INIT, _bind(1, 100))
    _claim(db, "a")
    assert _advance(db, "b", 1, 200, 100) == 0, "wrong owner"
    assert _advance(db, "a", 2, 200, 100) == 0, "wrong epoch"
    assert _advance(db, "a", 1, 200, 50) == 0, "the window did not start there"
    assert _rows(db, 1)[0][0] == 100
    assert _advance(db, "a", 1, 200, 100) == 1
    assert _rows(db, 1)[0][0] == 200
    assert _advance(db, "a", 1, 300, 100) == 0, "a replayed advance does not move it again"


def test_a_stale_owner_cannot_advance_after_a_takeover(db: Any) -> None:
    db.execute(INIT, _bind(1, 100))
    _claim(db, "a")  # epoch 1; a then stalls
    db.execute("UPDATE assembler_partitions SET lease_until = now() - interval '1 second'")
    assert _claim(db, "b") == (2, 100)
    assert _advance(db, "a", 1, 200, 100) == 0, "a's late commit is fenced"
    assert _advance(db, "b", 2, 200, 100) == 1
    assert _rows(db, 1)[0] == (200, "b", 2)


def test_the_advance_renews_the_lease(db: Any) -> None:
    db.execute(INIT, _bind(1, 100))
    _claim(db, "a", ttl=1)
    assert _advance(db, "a", 1, 200, 100, ttl=60) == 1
    (alive,) = db.execute(
        "SELECT lease_until > now() + interval '30 seconds' FROM assembler_partitions"
    ).fetchone()
    assert alive


def test_the_schema_rejects_a_partition_outside_its_count(db: Any) -> None:
    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute(
            "INSERT INTO assembler_partitions (partition_count, partition, watermark_ms) "
            "VALUES (2, 2, 0)"
        )


# --- replicas and the release of a partition (fair share) ---------------------------------------


def test_a_replica_is_alive_while_its_heartbeat_is_inside_the_window(db: Any) -> None:
    for owner in ("a", "b", "c"):
        db.execute(HEARTBEAT, _bind(owner))
    db.execute(
        "UPDATE assembler_replicas SET heartbeat_at = now() - interval '45 seconds' "
        "WHERE owner = 'c'"
    )

    def live(window: float) -> int:
        return int(db.execute(LIVE, _bind(window)).fetchone()[0])

    assert live(30) == 2, "c has not been seen for 45 s"
    assert live(60) == 3
    db.execute(HEARTBEAT, _bind("c"))
    assert live(30) == 3, "a heartbeat revives the row"
    assert int(db.execute("SELECT count(*) FROM assembler_replicas").fetchone()[0]) == 3, "upsert"


def _owners(db: Any) -> list[str]:
    return sorted(r[0] for r in db.execute("SELECT owner FROM assembler_replicas").fetchall())


def test_a_dead_owners_row_disappears_and_a_live_one_stays(db: Any) -> None:
    ttl = 30
    for owner in ("dead", "silent", "alive"):
        db.execute(HEARTBEAT, _bind(owner))
    # 20 min and 2 min without a heartbeat; the prune age is 10 x the TTL = 300 s
    db.execute(
        "UPDATE assembler_replicas SET heartbeat_at = now() - interval '20 minutes' "
        "WHERE owner = 'dead'"
    )
    db.execute(
        "UPDATE assembler_replicas SET heartbeat_at = now() - interval '2 minutes' "
        "WHERE owner = 'silent'"
    )
    db.execute(PRUNE, _bind(10 * ttl, 100))
    assert _owners(db) == ["alive", "silent"], (
        "dead is gone; silent is not live but not yet forgotten"
    )
    live = int(db.execute(LIVE, _bind(ttl)).fetchone()[0])
    assert live == 1, "only the replica with a fresh heartbeat counts"
    db.execute(PRUNE, _bind(10 * ttl, 100))
    assert _owners(db) == ["alive", "silent"], "pruning twice changes nothing (idempotent)"


def test_the_prune_is_bounded_and_oldest_first(db: Any) -> None:
    for i in range(150):
        db.execute(HEARTBEAT, _bind(f"old-{i:03d}"))
    db.execute(HEARTBEAT, _bind("alive"))
    db.execute(
        "UPDATE assembler_replicas SET heartbeat_at = now() - interval '1 hour' "
        "- (substr(owner, 5)::integer || ' seconds')::interval WHERE owner LIKE 'old-%'"
    )
    db.execute(PRUNE, _bind(300, 100))
    assert len(_owners(db)) == 51, "one call removes at most 100 rows"
    left = [o for o in _owners(db) if o != "alive"]
    assert left == [f"old-{i:03d}" for i in range(50)], "the oldest rows went first"
    db.execute(PRUNE, _bind(300, 100))
    assert _owners(db) == ["alive"], "the next heartbeat finishes the job"


def test_a_replica_that_heartbeats_while_a_prune_runs_is_not_deleted(pg_database: str) -> None:
    """The prune picks stale rows in a subquery. A replica revived between that read and the
    delete (its upsert holds the row lock, the delete waits for it) must keep its row: the
    delete re-checks the heartbeat once it gets the lock."""
    with connect_postgres(pg_database) as conn:
        migrate(conn, packaged(POSTGRES))
    with (
        psycopg.connect(pg_database, autocommit=True) as setup_conn,
        psycopg.connect(pg_database) as reviver_conn,
        psycopg.connect(pg_database, autocommit=True) as pruner_conn,
        psycopg.connect(pg_database, autocommit=True) as watcher_conn,
    ):
        setup: Any = setup_conn
        reviver: Any = reviver_conn
        pruner: Any = pruner_conn
        watcher: Any = watcher_conn
        setup.execute(HEARTBEAT, _bind("revived"))
        setup.execute(HEARTBEAT, _bind("dead"))
        setup.execute("UPDATE assembler_replicas SET heartbeat_at = now() - interval '1 hour'")

        reviver.execute(HEARTBEAT, _bind("revived"))  # uncommitted: it holds the row lock
        outcome: list[int] = []

        def prune() -> None:
            outcome.append(pruner.execute(PRUNE, _bind(300, 100)).rowcount)

        t = threading.Thread(target=prune)
        t.start()
        try:
            deadline = time.monotonic() + 20
            while True:  # the prune has read its subquery and now waits for the lock
                waiting = watcher.execute(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE wait_event_type = 'Lock' AND pid <> pg_backend_pid() "
                    "AND query LIKE '%DELETE FROM assembler_replicas%'"
                ).fetchone()
                if waiting[0] == 1:
                    break
                assert time.monotonic() < deadline, "the prune never blocked on the row lock"
                time.sleep(0.05)
        finally:
            reviver.commit()  # releases the lock whatever happened above
            t.join(timeout=20)
        assert not t.is_alive()

        owners = sorted(
            r[0] for r in setup.execute("SELECT owner FROM assembler_replicas").fetchall()
        )
        assert owners == ["revived"], "the dead replica is pruned, the revived one is not"
        assert outcome == [1]


def test_liveness_ignores_the_clients_clock(pg_database: str) -> None:
    """The heartbeat and the live count use now() on the server: clients whose sessions claim
    different time zones are judged by the one server clock."""
    with connect_postgres(pg_database) as conn:
        migrate(conn, packaged(POSTGRES))
    with (
        psycopg.connect(pg_database, autocommit=True) as conn_a,
        psycopg.connect(pg_database, autocommit=True) as conn_b,
    ):
        a: Any = conn_a  # the statements are files read at run time, not literals
        b: Any = conn_b
        a.execute("SET TIME ZONE 'Pacific/Kiritimati'")
        b.execute("SET TIME ZONE 'Etc/GMT+12'")
        a.execute(HEARTBEAT, _bind("a"))
        b.execute(HEARTBEAT, _bind("b"))
        live = a.execute(LIVE, _bind(30)).fetchone()
        assert live is not None
        assert live[0] == 2
        skew = a.execute(
            "SELECT max(abs(extract(epoch FROM heartbeat_at - now()))) FROM assembler_replicas"
        ).fetchone()
        assert skew is not None
        assert float(skew[0]) < 5, "both heartbeats carry the server's now(), whatever the zone"


def test_a_release_frees_the_partition_keeps_the_watermark_and_is_fenced_by_epoch(db: Any) -> None:
    db.execute(INIT, _bind(1, 100))
    _claim(db, "a")  # epoch 1
    assert _advance(db, "a", 1, 200, 100) == 1
    assert db.execute(RELEASE, _bind(1, 0, "b", 1)).rowcount == 0, "not the owner"
    assert db.execute(RELEASE, _bind(1, 0, "a", 7)).rowcount == 0, "not that epoch"
    assert db.execute(RELEASE, _bind(1, 0, "a", 1)).rowcount == 1
    assert _rows(db, 1)[0] == (200, None, 1), "free, watermark kept"
    assert _claim(db, "b") == (2, 200), "the next owner resumes at the watermark with a new epoch"
    assert _advance(db, "a", 1, 300, 200) == 0, "a's late commit is fenced"
