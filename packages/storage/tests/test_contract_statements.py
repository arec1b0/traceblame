import re

from traceblame_storage.contract import packaged, parameters


def test_writes_come_before_reads_and_each_file_is_one_statement() -> None:
    names = [name for name, _ in packaged()]
    writes = [n for n in names if n.endswith("_write.sql")]
    assert names[: len(writes)] == writes
    assert {"spans_write.sql", "spans_read.sql", "findings_write.sql"} <= set(names)


def test_every_query_parameter_has_a_sample_value_for_the_n_minus_1_run() -> None:
    parametrised = 0
    for name, sql in packaged():
        placeholders = set(re.findall(r"\{([A-Za-z_][A-Za-z0-9_]*):[A-Za-z0-9(), ]+\}", sql))
        assert placeholders == set(parameters(sql)), name
        parametrised += bool(placeholders)
    assert parametrised >= 2, "the trace-assembler's two reads take parameters"


def test_the_assemblers_statements_are_in_the_contract() -> None:
    names = {name for name, _ in packaged()}
    assert {"trace_arrivals_scan.sql", "spans_by_trace_read.sql"} <= names


def test_parameters_are_json_values() -> None:
    sql = '-- param a = 1\n-- param b = ["x"]\n-- param c = "s"\nSELECT 1'
    assert parameters(sql) == {"a": 1, "b": ["x"], "c": "s"}
