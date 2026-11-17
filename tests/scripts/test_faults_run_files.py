"""What an attempt of a resumable run reads and keeps of its own files (#70; both found in
review of PR #71).

- The fingerprint a resume compares hashes plan.jsonl and queries.jsonl as they are on disk,
  not the hashes state.json recorded at prepare: an edited plan file is a changed provenance.
- A resume keeps the tee's copy of the last attempt's spans when that attempt did not
  complete, whether it was killed or failed, and only the spans of this run.
"""

import hashlib
import json
from pathlib import Path
from typing import Any

import faultslib  # tools/faults, on pytest's pythonpath as at the driver's run time
import pytest

RUN = "faults-d1-20261117-s19-abc1234"
A1, A2, A3 = "a001-20261117T100000Z", "a002-20261117T110000Z", "a003-20261117T120000Z"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _prepared(tmp_path: Path, *, queries: bool = True) -> tuple[Path, dict[str, Any]]:
    plan_text, queries_text = '{"entry":1}\n', '{"query":1}\n'
    (tmp_path / "plan.jsonl").write_text(plan_text, encoding="utf-8")
    (tmp_path / "queries.jsonl").write_text(queries_text, encoding="utf-8")
    state: dict[str, Any] = {"plan": {"sha256": _sha(plan_text)}}
    if queries:
        state["queries"] = {"sha256": _sha(queries_text)}
    return tmp_path, state


def test_the_hashes_are_of_the_files_on_disk(tmp_path: Path) -> None:
    d, state = _prepared(tmp_path)
    assert faultslib.run_file_hashes(d, state) == (
        state["plan"]["sha256"],
        state["queries"]["sha256"],
    )
    # A run prepared before the queries hash was recorded: hashed all the same.
    d, older = _prepared(tmp_path, queries=False)
    assert faultslib.run_file_hashes(d, older)[1] == state["queries"]["sha256"]


@pytest.mark.parametrize("name", ["plan.jsonl", "queries.jsonl"])
def test_a_plan_or_queries_file_edited_after_prepare_is_refused(tmp_path: Path, name: str) -> None:
    d, state = _prepared(tmp_path)
    (d / name).write_text((d / name).read_text(encoding="utf-8") + '{"edited":1}\n', "utf-8")
    with pytest.raises(faultslib.RunError, match=rf"{name} .*changed since the run was prepared"):
        faultslib.run_file_hashes(d, state)


def _events(*statuses: str | None) -> list[dict[str, Any]]:
    """attempt_start for A1, A2, ... each followed by its end (None = still open)."""
    out: list[dict[str, Any]] = []
    for attempt_id, status in zip((A1, A2, A3), statuses, strict=False):
        out.append({"event": "attempt_start", "at": "t", "attempt_id": attempt_id, "pid": 1})
        if status == "killed":
            out.append({"event": "attempt_killed", "at": "t", "attempt_id": attempt_id,
                        "status": "killed/unknown"})  # fmt: skip
        elif status is not None:
            out.append({"event": "attempt_end", "at": "t", "attempt_id": attempt_id,
                        "status": status})  # fmt: skip
    return out


@pytest.mark.parametrize(
    ("statuses", "current", "expected"),
    [
        ((None,), A1, None),  # a first attempt: nothing before it
        (("completed", None), A2, None),  # the attempt before wrote its own copies
        (("killed", None), A2, A1),
        (("failed: SystemExit", None), A2, A1),  # failed, not killed: its tee is unsaved too
        (("killed", "failed: CalledProcessError", None), A3, A2),  # the tee holds the last one
        (("failed: SystemExit", "completed", None), A3, None),
    ],
)
def test_the_tee_of_the_last_attempt_is_salvaged_unless_it_completed(
    statuses: tuple[str | None, ...], current: str, expected: str | None
) -> None:
    assert faultslib.attempt_to_salvage(_events(*statuses), current) == expected


def _resource_spans(run_id: str | None, span_id: str) -> dict[str, Any]:
    attrs = [{"key": "service.name", "value": {"stringValue": "runbook-agent"}}]
    if run_id is not None:
        attrs.append({"key": "aegis.eval.run.id", "value": {"stringValue": run_id}})
    return {
        "resource": {"attributes": attrs},
        "scopeSpans": [{"spans": [{"traceId": "a" * 32, "spanId": span_id}]}],
    }


def test_a_salvaged_tee_keeps_only_the_spans_of_this_run() -> None:
    """The tee pod can still hold the session of an earlier run when the attempt before this
    one never deployed its own."""
    otlp = {
        "resourceSpans": [
            _resource_spans(RUN, "1" * 16),
            _resource_spans("faults-20261105-s7-b1b6b60", "2" * 16),
            _resource_spans(None, "3" * 16),
            _resource_spans(RUN, "4" * 16),
        ]
    }
    own = faultslib.own_spans(otlp, RUN)
    kept = [
        s["spanId"] for rs in own["resourceSpans"] for ss in rs["scopeSpans"] for s in ss["spans"]
    ]
    assert kept == ["1" * 16, "4" * 16]
    assert faultslib.own_spans({"resourceSpans": []}, RUN) == {"resourceSpans": []}
    assert json.dumps(otlp).count(RUN) == 2, "the input is not changed"
