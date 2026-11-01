"""The positive export allowlist (ADR-0006 section 3). Anything not listed here is dropped.

Span keys are profile attributes (minus the ones only the normalizer sets) plus the keys the
normalizer's OpenInference mapping table reads or knows. tests/contract/test_shim_contract.py
checks that, and that no key here is content by the profile or any mapping table.
"""

import re

SPAN_KEYS = frozenset(
    {
        # Profile: GenAI semconv
        "gen_ai.operation.name",
        "gen_ai.provider.name",
        "gen_ai.request.model",
        "gen_ai.response.model",
        "gen_ai.request.max_tokens",
        "gen_ai.response.finish_reasons",
        "gen_ai.usage.input_tokens",
        "gen_ai.usage.output_tokens",
        "gen_ai.conversation.id",
        "server.address",
        "error.type",
        "http.response.status_code",
        "gen_ai.tool.name",
        "gen_ai.tool.call.id",
        "gen_ai.data_source.id",
        "gen_ai.agent.name",
        "gen_ai.agent.id",
        # Profile: aegis.* the shim sets
        "aegis.tool.args_hash",
        "aegis.retrieval.doc_ids",
        "aegis.retrieval.scores",
        "aegis.output.parse_ok",
        "aegis.output.cited_doc_ids",
        "aegis.session.id",
        "aegis.bundle.digest",
        "aegis.decision.id",
        "aegis.fault.injection.id",
        # OpenInference (collector/processor/normalizer/mappings/openinference/)
        "openinference.span.kind",
        "llm.system",
        "llm.model_name",
        "embedding.model_name",
        "llm.token_count.prompt",
        "llm.token_count.completion",
        "llm.token_count.total",
        "llm.token_count.prompt_details.cache_read",
        "llm.finish_reason",
        "input.mime_type",
        "output.mime_type",
        "tool.name",
        "agent.name",
    }
)
SPAN_PATTERNS = (re.compile(r"^retrieval\.documents\.\d+\.document\.(id|score)$"),)

RESOURCE_KEYS = frozenset(
    {
        "service.name",
        "service.version",
        "service.instance.id",
        "aegis.tenant.id",
        "aegis.eval.run.id",
        "telemetry.sdk.language",
        "telemetry.sdk.name",
        "telemetry.sdk.version",
    }
)

EXCEPTION_EVENT = "exception"
EXCEPTION_KEYS = frozenset({"exception.type"})

# The OpenAI client's "Error code: NNN" prefix, which the normalizer reads
# http.response.status_code from (#29). The body after it may quote the request.
STATUS_PREFIX = re.compile(r"^(?:[A-Za-z_][A-Za-z0-9_.]*: )?Error code: [0-9]{3}(?= - |$)")


def keep_span_key(key: str) -> bool:
    return key in SPAN_KEYS or any(p.fullmatch(key) for p in SPAN_PATTERNS)


def keep_resource_key(key: str) -> bool:
    return key in RESOURCE_KEYS


def status_prefix(description: str | None) -> str | None:
    if description is None:
        return None
    found = STATUS_PREFIX.match(description)
    return found.group(0) if found else None
