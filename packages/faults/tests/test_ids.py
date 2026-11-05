"""Injection ids, per-entry randomness and request seeds: deterministic, inside the profile
pattern of aegis.fault.injection.id."""

import hashlib
import re

import pytest

from traceblame_faults import ids

PROFILE_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
CUAD_QID = "LIMEENERGYCO_09_09_1999-EX-10-DISTRIBUTOR AGREEMENT__Document Name"


def test_injection_id_is_in_the_profile_pattern_even_for_a_cuad_qid_with_spaces() -> None:
    value = ids.injection_id("d1-20261106-s7-abc1234", "malformed_output", CUAD_QID)
    assert PROFILE_PATTERN.fullmatch(value)
    assert value.startswith("d1-20261106-s7-abc1234:malformed_output:")
    assert " " not in value


def test_injection_id_is_deterministic_and_query_dependent() -> None:
    a = ids.injection_id("run", "tool_failure", "q1")
    assert a == ids.injection_id("run", "tool_failure", "q1")
    assert a != ids.injection_id("run", "tool_failure", "q2")
    assert a != ids.injection_id("run", "malformed_output", "q1")


def test_injection_id_refuses_a_run_id_outside_the_pattern() -> None:
    with pytest.raises(ids.IdError):
        ids.injection_id("run with space", "tool_failure", "q1")
    with pytest.raises(ids.IdError):
        ids.injection_id("r" * 120, "golden_context_removed", "q1")


@pytest.mark.parametrize("bad", ["", "a b", "x" * 129, "ü", "a/b"])
def test_check_refuses_values_the_normalizer_would_drop(bad: str) -> None:
    with pytest.raises(ids.IdError):
        ids.check(bad, "test")


def test_entry_rng_is_reproducible_by_seed_query_and_kind() -> None:
    draw = [ids.entry_rng(7, "q1", "tool_failure").random() for _ in range(2)]
    assert draw[0] == draw[1]
    assert (
        ids.entry_rng(7, "q1", "tool_failure").random()
        != ids.entry_rng(8, "q1", "tool_failure").random()
    )
    assert (
        ids.entry_rng(7, "q1", "tool_failure").random()
        != ids.entry_rng(7, "q2", "tool_failure").random()
    )


def test_request_seed_is_a_31_bit_int_fixed_by_its_inputs() -> None:
    s = ids.request_seed(7, "q1", 1)
    assert s == ids.request_seed(7, "q1", 1)
    assert 0 <= s < 2**31
    assert s != ids.request_seed(7, "q1", 2)
    # The TypeScript preload computes the same value (vectors/mutations.json).
    assert s == int(hashlib.sha256(b"7:q1:1").hexdigest()[:8], 16) & 0x7FFFFFFF


def test_rank_orders_by_seed() -> None:
    assert ids.rank(7, "a", "b") == ids.rank(7, "a", "b")
    assert ids.rank(7, "a", "b") != ids.rank(8, "a", "b")
