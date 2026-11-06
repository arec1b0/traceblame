"""What a detector may read of a span's attrs map (CLAUDE.md rules 2 and 9).

The trace view exposes the typed columns of `spans` and, from its attrs map, only attributes a
detector declared. A declaration must name a span attribute of the pinned profile that is not a
column, and never a denied one:
- eval and fault markers (`aegis.eval.*`, `aegis.fault.*`): they are the labels;
- `exception.*`: an exception message can carry an injected error text or content;
- content and content references (`aegis.content.*`, the profile's forbidden attributes);
- correlation ids (session, conversation, decision): a detector judges a trace, not a user.

tests/contract/test_detector_policy.py checks that DECLARABLE, COLUMN_ATTRS and DENIED_ATTRS
partition the profile's span attributes exactly, and that COLUMN_ATTRS is what the spans view
maps into columns.
"""

from enum import StrEnum


class AttrType(StrEnum):
    STRING = "string"
    INT = "int"
    BOOL = "boolean"
    STRING_LIST = "string[]"


# Span attributes of profile v1 a detector may declare, with their profile type.
DECLARABLE: dict[str, AttrType] = {
    "gen_ai.response.model": AttrType.STRING,
    "gen_ai.request.max_tokens": AttrType.INT,
    "server.address": AttrType.STRING,
    "gen_ai.tool.call.id": AttrType.STRING,
    "gen_ai.data_source.id": AttrType.STRING,
    "gen_ai.agent.name": AttrType.STRING,
    "gen_ai.agent.id": AttrType.STRING,
    "aegis.output.parse_ok": AttrType.BOOL,
    "aegis.output.cited_doc_ids": AttrType.STRING_LIST,
}

# Span attributes the spans view stores in their own columns (migration 0003).
COLUMN_ATTRS = frozenset(
    {
        "gen_ai.operation.name",
        "aegis.span.kind",
        "gen_ai.provider.name",
        "gen_ai.request.model",
        "gen_ai.response.finish_reasons",
        "gen_ai.usage.input_tokens",
        "gen_ai.usage.output_tokens",
        "gen_ai.tool.name",
        "aegis.tool.args_hash",
        "aegis.retrieval.scores",
        "aegis.retrieval.doc_ids",
        "aegis.bundle.digest",
        "aegis.session.id",
        "aegis.content.ref",
        "aegis.profile.version",
        "aegis.decision.id",
        "error.type",
        "http.response.status_code",
        "aegis.source.sdk",
        "aegis.normalizer.version",
    }
)

DENIED_PREFIXES = ("aegis.eval.", "aegis.fault.", "aegis.content.", "exception.")
DENIED_ATTRS = frozenset(
    {
        "aegis.eval.run.id",
        "aegis.fault.injection.id",
        "aegis.content.ref",
        "aegis.session.id",
        "aegis.decision.id",
        "gen_ai.conversation.id",
        "exception.message",
        "exception.stacktrace",
        "exception.type",
        # The profile's forbidden attributes: raw content.
        "gen_ai.input.messages",
        "gen_ai.output.messages",
        "gen_ai.system_instructions",
        "gen_ai.tool.call.arguments",
        "gen_ai.tool.call.result",
        "gen_ai.retrieval.query.text",
        "gen_ai.retrieval.documents",
        "gen_ai.prompt",
        "gen_ai.completion",
    }
)


def is_denied(key: str) -> bool:
    return key in DENIED_ATTRS or key.startswith(DENIED_PREFIXES)


def declarable(key: str) -> bool:
    return key in DECLARABLE and not is_denied(key)
