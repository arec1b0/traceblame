"""Runs inside the agentic-rag capture environment (#14): the injection plan through the real app.

  queries  CUAD questions (cuad.sample), each contract chunked by the app's own chunker and its
           golden chunks mapped (traceblame_faults.golden); JSON lines of plan.Query, no text.
  run      every plan entry through the real RAGAgent and its tools, traced by traceblame-py,
           the entry's fault injected at its seam; one ledger record per trace, and one outcome
           line per entry on stdout (ids, counts and timings, no text).

Seams (no app code changes; ADR draft "Fault taxonomy, injection points and eval ground truth"):
- the agent's, the judge's and the summarizer's AsyncOpenAI clients get an httpx2 transport that
  adds a request seed to every chat call, and for the entry's call rewrites the request
  (max_tokens) or the response (malformed content); OpenInference runs the transport inside the
  LLM span, which is the span marked;
- the store behind the retriever (golden chunks removed, or a backend error);
- instance attributes of the one agent: _generate_step (memory loss), the prompt templates, and
  _reasoning_loop, which reads the trace id and marks the AGENT span. Everything is undone
  after each entry.

The driver (tools/faults/run_agentic_rag.py) sets the environment as capture_agentic_rag.py
does, plus TRACEBLAME_FAULTS=1 and TRACEBLAME_EVAL_TENANTS: the guard refuses anything else.
"""

import argparse
import asyncio
import json
import os
import sys
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

# Run with `python -I`, which ignores PYTHONPATH: the driver names the app checkout instead.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "fixtures" / "agentic_rag"))
sys.path.insert(0, os.environ["CAPTURE_APP_DIR"])

import cuad
import httpx2
import run as app_run  # tools/fixtures/agentic_rag/run.py: chunks, session ids, quiet logs
from opentelemetry import trace

import traceblame_py
from traceblame_faults import bundle, golden, guard, ids, kinds, ledger, mutate, plan, retrieval

APP = "agentic-rag"
CHAT_PATH = "/chat/completions"
HTTP_OK = 200
_DROP_RESPONSE_HEADERS = {"content-length", "content-encoding", "transfer-encoding"}


def _now() -> str:
    return ledger.now_iso(datetime.now(UTC))


def _trace_id(span: trace.Span) -> str:
    return format(span.get_span_context().trace_id, "032x")


class Injection:
    """One plan entry while its trace runs: what fired, where, and the marked spans."""

    def __init__(self, entry: plan.Entry, query: plan.Query, baseline: str) -> None:
        self.entry = entry
        self.query = query
        self.baseline = baseline
        self.bundle_digest = baseline
        self.trace_id: str | None = None
        self.applied: dict[str, Any] | None = None
        self.injected_at: str | None = None
        self.marked: dict[int, str] = {}
        self.calls: dict[str, int] = {}

    def fire(self, span: trace.Span, extra: dict[str, Any] | None = None) -> None:
        if self.applied is None:
            self.applied = {}
            self.injected_at = _now()
        for key, value in (extra or {}).items():
            if key in ("removed_n", "retrievals"):
                self.applied[key] = int(self.applied.get(key, 0)) + int(value)
            else:
                self.applied[key] = value
        span_id = span.get_span_context().span_id
        if span_id not in self.marked:
            traceblame_py.set_fault_injection_id(self.entry.injection_id, span)
            self.marked[span_id] = getattr(span, "name", "")

    def record(self) -> ledger.Record:
        e, q = self.entry, self.query
        assert self.trace_id is not None  # noqa: S101 - checked by the caller
        common: dict[str, Any] = {
            "trace_id": self.trace_id,
            "eval_run_id": os.environ["TRACEBLAME_EVAL_RUN_ID"],
            "service": APP,
            "source": "cuad",
            "query_ref": q.ref,
            "stratum": q.stratum,
            "golden_doc_ids": q.golden_doc_ids if q.golden_status == "mapped" else (),
            "golden_status": q.golden_status,
            "planned_fault_kind": e.kind,
            "seed": e.seed,
            "bundle_digest": self.bundle_digest,
            "host_load1": ledger.host_load1(),  # recorded when the trace has ended
        }
        if self.applied is None:
            return ledger.Record(
                **common, injection_id="", fault_kind="", target_component="", params={}
            )
        return ledger.Record(
            **common,
            injection_id=e.injection_id,
            fault_kind=e.kind,
            target_component=kinds.KINDS[e.kind].component,
            params=dict(e.params) | self.applied | {"baseline_digest": self.baseline},
            injected_at=self.injected_at,
        )


class Transport(httpx2.AsyncBaseTransport):
    """The OpenAI client's transport: a request seed on every chat call; the entry's LLM-seam
    fault on its call (namespace "agent" only)."""

    def __init__(self, inj: Injection, namespace: str) -> None:
        self._inner = httpx2.AsyncHTTPTransport()
        self._inj = inj
        self._ns = namespace

    def _due(self, n: int) -> bool:
        e = self._inj.entry
        if self._ns != "agent" or e.kind not in ("malformed_output", "output_budget_exhausted"):
            return False
        k = int(e.params["call_index"])
        return n == k or (e.params["persist"] == "all" and n >= k)

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        if request.method != "POST" or not request.url.path.endswith(CHAT_PATH):
            return await self._inner.handle_async_request(request)
        inj, e = self._inj, self._inj.entry
        n = inj.calls[self._ns] = inj.calls.get(self._ns, 0) + 1
        ref = e.query_ref if self._ns == "agent" else f"{e.query_ref}#{self._ns}"
        fields: dict[str, Any] = {"seed": ids.request_seed(e.seed, ref, n)}
        due = self._due(n)
        span = trace.get_current_span()
        if due and e.kind == "output_budget_exhausted":
            fields["max_tokens"] = int(e.params["max_tokens"])
            inj.fire(span)
        headers = [
            (k, v) for k, v in request.headers.multi_items() if k.lower() != "content-length"
        ]
        sent = httpx2.Request(
            request.method,
            request.url,
            headers=headers,
            content=mutate.with_fields(request.content, fields),
            extensions=request.extensions,
        )
        response = await self._inner.handle_async_request(sent)
        if not (due and e.kind == "malformed_output") or response.status_code != HTTP_OK:
            return response
        raw = await response.aread()
        await response.aclose()
        body = mutate.malform(raw, str(e.params["mode"]), float(e.params["cut_fraction"]))
        inj.fire(span)
        kept = [
            (k, v)
            for k, v in response.headers.multi_items()
            if k.lower() not in _DROP_RESPONSE_HEADERS
        ]
        return httpx2.Response(response.status_code, headers=kept, content=body, request=sent)

    async def aclose(self) -> None:
        await self._inner.aclose()


def _client(inj: Injection, namespace: str) -> Any:
    from openai import AsyncOpenAI

    return AsyncOpenAI(
        api_key=os.environ["OPENAI_API_KEY"],
        http_client=httpx2.AsyncClient(transport=Transport(inj, namespace), timeout=600),
    )


# Queries ----------------------------------------------------------------------------------


def _queries(args: argparse.Namespace) -> int:
    items = cuad.sample(
        cuad.load_test_split(Path(args.archive)), n=args.n, seed=args.seed, ratio=args.ratio
    )
    if args.stratum != "all":
        items = [i for i in items if i.answerable == (args.stratum == "answerable")]
    out: list[plan.Query] = []
    for item in items:
        chunk_ids = golden.golden_chunks(
            item.answers, [(c.doc_id, c.content) for c in app_run.chunks(item)]
        )
        q = plan.Query(
            ref=item.qid,
            app=APP,
            source="cuad",
            stratum="answerable" if item.answerable else "unanswerable",
            golden_status=golden.status(answerable=item.answerable, chunk_ids=chunk_ids),
            golden_doc_ids=tuple(chunk_ids),
        )
        if args.require_mapped and q.golden_status != "mapped":
            continue
        out.append(q)
        if args.limit and len(out) >= args.limit:
            break
    sys.stdout.write(plan.dump_queries(out))
    return 0


# Run --------------------------------------------------------------------------------------


class Stores:
    """One Chroma collection per contract, built once per process."""

    def __init__(self, by_title: dict[str, cuad.Item]) -> None:
        self._by_title = by_title
        self._stores: dict[str, Any] = {}

    def get(self, title: str) -> Any:
        from src.vectordb import ChromaStore

        if title not in self._stores:
            item = self._by_title[title]
            store = ChromaStore(collection_name=cuad.doc_id(title, 0))
            store.add_documents(app_run.chunks(item))
            self._stores[title] = store
        return self._stores[title]


def _contract_id(title: str) -> str:
    return cuad.doc_id(title, 0).rsplit("-", 1)[0]


def _distractor_title(seed: int, ref: str, title: str, titles: list[str]) -> str:
    others = sorted((t for t in titles if t != title), key=lambda t: ids.rank(seed, ref, t))
    return others[0]


def _install(
    agent: Any,
    inj: Injection,
    store: Any,
    stores: Stores,
    item: cuad.Item,
    titles: list[str],
    variants: dict[str, dict[str, str]],
) -> Callable[[], Awaitable[None]]:
    """Wire the entry's seams into the agent; returns the undo."""
    e = inj.entry
    retriever = agent.tool_selector.get_tool("retriever")
    summarizer = agent.tool_selector.get_tool("summarizer")
    originals = {
        "agent_client": agent.client,
        "judge_client": agent.llm_judge.client if agent.llm_judge else None,
        "summarizer_client": summarizer.client if summarizer else None,
    }
    agent.client = _client(inj, "agent")
    if agent.llm_judge:
        agent.llm_judge.client = _client(inj, "judge")
    if summarizer:
        summarizer.client = _client(inj, "summarizer")
    view: Any = store
    agent_attrs: list[str] = []

    orig_loop = agent._reasoning_loop

    async def loop(*args: Any, **kwargs: Any) -> Any:
        span = trace.get_current_span()
        inj.trace_id = _trace_id(span)
        if e.kind == "prompt_variant":
            inj.fire(span, {"bundle_digest": inj.bundle_digest})
        return await orig_loop(*args, **kwargs)

    agent._reasoning_loop = loop
    agent_attrs.append("_reasoning_loop")

    if e.kind == "golden_context_removed":
        golden_ids = frozenset(inj.query.golden_doc_ids)

        def applied(extra: dict[str, Any]) -> None:
            inj.fire(trace.get_current_span(), extra | {"retrievals": 1})

        if e.params["mode"] == "distractor":
            other = _distractor_title(e.seed, e.query_ref, item.title, titles)
            view = retrieval.GoldenRemoved(
                store,
                golden_ids,
                on_applied=applied,
                distractor=stores.get(other),
                distractor_doc=_contract_id(other),
            )
        else:
            view = retrieval.GoldenRemoved(store, golden_ids, on_applied=applied)
    elif e.kind == "tool_failure":
        failing = retrieval.FailOnce(store)
        view = failing
        orig_execute = retriever.execute
        count = {"n": 0}

        async def execute(*args: Any, **kwargs: Any) -> Any:
            count["n"] += 1
            if count["n"] == int(e.params["call_index"]):
                inj.fire(trace.get_current_span())
                failing.arm(str(e.params["mode"]))
            return await orig_execute(*args, **kwargs)

        retriever.execute = execute
    elif e.kind == "step_memory_loss":
        orig_step = agent._generate_step
        state: dict[str, Any] = {"n": 0, "first": None}

        async def step(*, query: str, previous_steps: Any, observation: str) -> Any:
            state["n"] += 1
            if state["n"] == 1:
                state["first"] = observation
            if state["n"] >= int(e.params["from_iteration"]):
                inj.fire(trace.get_current_span())
                return await orig_step(query=query, previous_steps=[], observation=state["first"])
            return await orig_step(
                query=query, previous_steps=previous_steps, observation=observation
            )

        agent._generate_step = step
        agent_attrs.append("_generate_step")
    elif e.kind == "prompt_variant":
        templates = variants[str(e.params["variant"])]
        if "system" in templates:
            agent.SYSTEM_PROMPT = templates["system"]
            agent_attrs.append("SYSTEM_PROMPT")
        if "reasoning" in templates:
            agent.REASONING_PROMPT = templates["reasoning"]
            agent_attrs.append("REASONING_PROMPT")
        inj.bundle_digest = bundle.digest(
            APP, {"system": agent.SYSTEM_PROMPT, "reasoning": agent.REASONING_PROMPT}
        )

    agent.vector_store = view
    retriever.vector_store = view

    async def undo() -> None:
        for name in agent_attrs:
            delattr(agent, name)
        if e.kind == "tool_failure":
            del retriever.execute
        await agent.client.close()
        agent.client = originals["agent_client"]
        if agent.llm_judge:
            await agent.llm_judge.client.close()
            agent.llm_judge.client = originals["judge_client"]
        if summarizer:
            await summarizer.client.close()
            summarizer.client = originals["summarizer_client"]

    return undo


async def _run(args: argparse.Namespace) -> int:
    from src.agents.rag_agent import RAGAgent
    from src.observability import aegis

    from traceblame_faults import scenario

    guard.require(os.environ)
    entries = plan.loads(Path(args.plan).read_text(encoding="utf-8"))
    queries = {q.ref: q for q in plan.load_queries(Path(args.queries).read_text(encoding="utf-8"))}
    split = cuad.load_test_split(Path(args.archive))
    items = {i.qid: i for i in split if i.qid in queries}
    titles = sorted({i.title for i in split})
    by_title = {i.title: i for i in split}
    stores = Stores(by_title)
    sc = scenario.load_all().get(("prompt_variant", APP))
    variants: dict[str, dict[str, str]] = (
        {n: dict(v.templates) for n, v in sc.variants.items()} if sc else {}
    )
    baseline = bundle.digest(
        APP, {"system": RAGAgent.SYSTEM_PROMPT, "reasoning": RAGAgent.REASONING_PROMPT}
    )
    if not aegis.setup_tracing():
        raise SystemExit("run: tracing is off (TRACEBLAME_ENABLED=1 and traceblame-py needed)")
    ledger_path = Path(args.ledger)
    agent: Any = None
    try:
        for e in entries:
            q, item = queries[e.query_ref], items[e.query_ref]
            store = stores.get(item.title)
            if agent is None:
                # One agent per process: the app registers its Prometheus metrics globally.
                agent = RAGAgent(vector_store=store, enable_tracing=False)
            inj = Injection(e, q, baseline)
            undo = _install(agent, inj, store, stores, item, titles, variants)
            started = time.monotonic()
            outcome: dict[str, Any] = {"query_ref": e.query_ref, "kind": e.kind}
            try:
                with traceblame_py.bundle(inj.bundle_digest):
                    response = await agent.run(
                        item.question, context={"session_id": app_run.session_id(item.qid)}
                    )
                outcome |= {
                    "outcome": "answered" if response.answer else "empty_answer",
                    "steps": [s.action for s in response.steps],
                }
            except Exception as exc:
                outcome["outcome"] = f"error:{type(exc).__name__}"
            finally:
                await undo()
            outcome["seconds"] = round(time.monotonic() - started, 2)
            outcome["llm_calls"] = dict(inj.calls)
            outcome["marked_spans"] = len(inj.marked)
            if inj.trace_id is None:
                outcome["status"] = "no_trace"
            else:
                rec = inj.record()
                ledger.append(ledger_path, rec)
                outcome |= {"trace_id": inj.trace_id, "status": ledger.status(rec)}
            print(json.dumps(outcome), flush=True)
    finally:
        aegis.shutdown_tracing()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=cast("str", __doc__).splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    q = sub.add_parser("queries")
    q.add_argument("--archive", required=True)
    q.add_argument("--n", type=int, required=True)
    q.add_argument("--seed", type=int, required=True)
    q.add_argument("--ratio", type=float, default=0.5)
    q.add_argument("--stratum", choices=["all", "answerable", "unanswerable"], default="all")
    q.add_argument("--require-mapped", action="store_true")
    q.add_argument("--limit", type=int, default=0)
    r = sub.add_parser("run")
    r.add_argument("--archive", required=True)
    r.add_argument("--plan", required=True)
    r.add_argument("--queries", required=True)
    r.add_argument("--ledger", required=True)
    args = parser.parse_args()
    app_run.quiet_logs()
    if args.command == "queries":
        return _queries(args)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
