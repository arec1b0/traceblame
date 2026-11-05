"""Ids and seeded randomness. Everything here is a pure function of its inputs, so the same
seed gives the same fault choice, parameters and injection point (#14)."""

import hashlib
import random
import re

# aegis.fault.injection.id and aegis.eval.run.id (profile 1.5.0): the normalizer drops any
# other value, so a value outside it is a harness bug, raised here.
PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_QUERY_HASH_LEN = 12


class IdError(ValueError):
    pass


def check(value: str, what: str) -> str:
    if not PATTERN.fullmatch(value):
        raise IdError(f"{what} {value!r} is outside {PATTERN.pattern}")
    return value


def query_hash(query_ref: str) -> str:
    """A short id of a dataset query id; CUAD ids carry spaces, which the pattern refuses."""
    return hashlib.sha256(query_ref.encode()).hexdigest()[:_QUERY_HASH_LEN]


def injection_id(eval_run_id: str, kind: str, query_ref: str) -> str:
    """`{eval_run_id}:{kind}:{sha256(query_ref)[:12]}`: one injection per query and kind."""
    check(eval_run_id, "eval_run_id")
    return check(f"{eval_run_id}:{kind}:{query_hash(query_ref)}", "injection_id")


def rank(seed: int, *parts: str) -> str:
    """Sort key of a seeded ranking, like cuad.sample's: sha256 of "seed:part:..."."""
    return hashlib.sha256(":".join([str(seed), *parts]).encode()).hexdigest()


def entry_rng(seed: int, query_ref: str, kind: str) -> random.Random:
    """The randomness of one plan entry: its parameters and injection point."""
    return random.Random(int(rank(seed, query_ref, kind)[:16], 16))  # noqa: S311 - not crypto


def request_seed(seed: int, query_ref: str, call_index: int) -> int:
    """The `seed` the harness adds to the call_index-th LLM request of a query's run (clean and
    faulted alike): a 31-bit int, the same in the TypeScript preload."""
    return int(rank(seed, query_ref, str(call_index))[:8], 16) & 0x7FFFFFFF
