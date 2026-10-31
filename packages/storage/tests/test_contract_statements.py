from traceblame_storage.contract import packaged


def test_writes_come_before_reads_and_each_file_is_one_statement() -> None:
    names = [name for name, _ in packaged()]
    writes = [n for n in names if n.endswith("_write.sql")]
    assert names[: len(writes)] == writes
    assert {"spans_write.sql", "spans_read.sql", "findings_write.sql"} <= set(names)
