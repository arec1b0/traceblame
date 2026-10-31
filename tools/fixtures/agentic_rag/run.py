"""Runs inside the capture environment, with the agentic-rag checkout on PYTHONPATH (#12).

  run            CUAD queries through the real app (RAGAgent, its tools, Chroma), traced by
                 traceblame-py. Prints one JSON line per query: ids and outcomes, no text.
  canary         The same app path against a stub LLM server whose answers, tool arguments
                 and error body carry canary strings; the driver checks none is exported.
  check-content  Fails if any query, or any 48-character window of any chunk of the queried
                 contracts, occurs in the strings of the given JSON files.

The driver (tools/fixtures/capture_agentic_rag.py) sets the environment: the app checkout
(CAPTURE_APP_DIR), OpenAI endpoint and models, OTLP endpoint, TRACEBLAME_* and the app's
switches. Nothing here logs content.
"""

import argparse
import asyncio
import hashlib
import json
import os
import sys
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

# Run with `python -I`, which ignores PYTHONPATH: the driver names the app checkout instead.
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, os.environ["CAPTURE_APP_DIR"])

import cuad

WINDOW = 48
STEP = 16


def _quiet_logs() -> None:
    """The app logs query prefixes through loguru; keep only its errors, without text."""
    from loguru import logger

    logger.remove()
    logger.add(sys.stderr, level="ERROR", format="{level} {name}:{line}")


def _chunks(item: cuad.Item) -> list[Any]:
    from src.vectordb import Document, DocumentProcessor

    chunks = DocumentProcessor().chunk_document(
        Document(content=item.context, metadata={"title": item.title})
    )
    for n, chunk in enumerate(chunks):
        chunk.doc_id = cuad.doc_id(item.title, n)
    return chunks


def _session(qid: str) -> str:
    return "s-" + hashlib.sha256(qid.encode()).hexdigest()[:16]


def _sampled(args: argparse.Namespace) -> list[cuad.Item]:
    items = cuad.sample(
        cuad.load_test_split(Path(args.archive)), n=args.n, seed=args.seed, ratio=args.ratio
    )
    if args.stratum != "all":
        items = [i for i in items if i.answerable == (args.stratum == "answerable")]
    return items[: args.limit] if args.limit else items


async def _run(args: argparse.Namespace) -> int:
    from src.agents.rag_agent import RAGAgent
    from src.observability import aegis
    from src.vectordb import ChromaStore

    if not aegis.setup_tracing():
        raise SystemExit("run: tracing is off (TRACEBLAME_ENABLED=1 and traceblame-py needed)")
    agents: dict[str, Any] = {}
    try:
        for item in _sampled(args):
            if item.title not in agents:
                store = ChromaStore(collection_name=cuad.doc_id(item.title, 0))
                store.add_documents(_chunks(item))
                agents[item.title] = RAGAgent(vector_store=store, enable_tracing=False)
            record: dict[str, Any] = {
                "qid": item.qid,
                "session": _session(item.qid),
                "answerable": item.answerable,
            }
            try:
                response = await agents[item.title].run(
                    item.question, context={"session_id": record["session"]}
                )
                record |= {
                    "outcome": "answered" if response.answer else "empty_answer",
                    "steps": [s.action for s in response.steps],
                }
            except Exception as exc:
                record["outcome"] = f"error:{type(exc).__name__}"
            print(json.dumps(record), flush=True)
    finally:
        aegis.shutdown_tracing()
    return 0


# Canary preflight -------------------------------------------------------------------------

CANARIES = {
    "query": "CANARY-QUERY-5d1e lease renewal term of the canary agreement",
    "document": "CANARY-DOC-77ab",
    "thought": "CANARY-THOUGHT-0c2f",
    "tool_args": "CANARY-ARGS-9e41",
    "answer": "CANARY-ANSWER-b3d0",
    "error": "CANARY-ERROR-4a7c",
}
CANARY_TITLE = "traceblame canary contract"


class _StubLLM(BaseHTTPRequestHandler):
    """An OpenAI-compatible chat endpoint with scripted answers (a test double, not data)."""

    reasoning_calls = 0
    doc_id = ""

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        prompt = " ".join(str(m.get("content")) for m in body["messages"])
        if "Previous steps:" not in prompt:
            # Anything but the reasoning loop (the judge): an error whose body is content.
            self._reply(
                400,
                {
                    "error": {
                        "message": f"bad request {CANARIES['error']}",
                        "type": "invalid_request_error",
                    }
                },
            )
            return
        cls = type(self)
        cls.reasoning_calls += 1
        steps = [
            {"action": "retriever", "action_input": {"query": CANARIES["query"]}},
            {"action": "calculator", "action_input": {"expression": f"{CANARIES['tool_args']} +"}},
            {
                "action": "final_answer",
                "action_input": {"answer": f"{CANARIES['answer']} [{cls.doc_id}]"},
            },
        ]
        step = steps[min(cls.reasoning_calls, len(steps)) - 1] | {
            "thought": CANARIES["thought"],
            "confidence": 0.9,
        }
        message = {"role": "assistant", "content": json.dumps(step)}
        self._reply(
            200,
            {
                "id": f"chatcmpl-{cls.reasoning_calls}",
                "object": "chat.completion",
                "created": 1,
                "model": body["model"],
                "choices": [{"index": 0, "finish_reason": "stop", "message": message}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
            },
        )

    def _reply(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


async def _canary(_: argparse.Namespace) -> int:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _StubLLM)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    os.environ["OPENAI_BASE_URL"] = f"http://127.0.0.1:{server.server_address[1]}/v1"

    from src.agents.rag_agent import RAGAgent
    from src.observability import aegis
    from src.vectordb import ChromaStore

    if not aegis.setup_tracing():
        raise SystemExit("canary: tracing is off")
    # The query is the document's text, so retrieval finds it above the app's threshold.
    text = f"{CANARIES['document']} {CANARIES['query']}. " * 6
    item = cuad.Item(qid="canary", title=CANARY_TITLE, question=text, answerable=True, context=text)
    chunks = _chunks(item)
    _StubLLM.doc_id = chunks[0].doc_id
    try:
        store = ChromaStore(collection_name="traceblame-canary")
        store.add_documents(chunks)
        agent = RAGAgent(vector_store=store, enable_tracing=False)
        response = await agent.run(text, context={"session_id": "s-canary"})
    finally:
        aegis.shutdown_tracing()
        server.shutdown()
    print(
        json.dumps(
            {
                "canaries": sorted(CANARIES.values()),
                "doc_id": _StubLLM.doc_id,
                "steps": [s.action for s in response.steps],
                "reasoning_calls": _StubLLM.reasoning_calls,
            }
        )
    )
    return 0


# Content check ----------------------------------------------------------------------------


def _strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for k, v in value.items():
            yield k
            yield from _strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v)


def _windows(text: str) -> Iterator[str]:
    # A leak of WINDOW + STEP - 1 characters or more contains one of these windows.
    for start in range(0, max(len(text) - WINDOW, 0) + 1, STEP):
        window = text[start : start + WINDOW]
        if window.strip():
            yield window


def _check_content(args: argparse.Namespace) -> int:
    wanted = set(args.qids)
    items = [i for i in cuad.load_test_split(Path(args.archive)) if i.qid in wanted]
    if {i.qid for i in items} != wanted:
        raise SystemExit("check-content: unknown qids")
    exported: set[str] = set()
    for path in args.files:
        for line in Path(path).read_text(encoding="utf-8").splitlines() or [""]:
            if not line.strip():
                continue
            for s in _strings(json.loads(line)):
                exported.update(s[i : i + WINDOW] for i in range(max(len(s) - WINDOW, 0) + 1))
    found: list[str] = []
    for item in items:
        texts = [item.question] + [c.content for c in _chunks(item)]
        if any(w in exported for t in texts for w in _windows(t)):
            found.append(item.qid)
    # Only the qids, never the text found.
    print(json.dumps({"checked_qids": len(items), "leaking_qids": found}))
    return 1 if found else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--archive", required=True)
    run.add_argument("--n", type=int, required=True)
    run.add_argument("--seed", type=int, required=True)
    run.add_argument("--ratio", type=float, default=0.5)
    run.add_argument("--stratum", choices=["all", "answerable", "unanswerable"], default="all")
    run.add_argument("--limit", type=int, default=0, help="only the first LIMIT sampled items")
    sub.add_parser("canary")
    check = sub.add_parser("check-content")
    check.add_argument("--archive", required=True)
    check.add_argument("--qids", nargs="+", required=True)
    check.add_argument("files", nargs="+", help="JSON or JSON-lines files")
    args = parser.parse_args()
    _quiet_logs()
    if args.command == "run":
        return asyncio.run(_run(args))
    if args.command == "canary":
        return asyncio.run(_canary(args))
    return _check_content(args)


if __name__ == "__main__":
    raise SystemExit(main())
