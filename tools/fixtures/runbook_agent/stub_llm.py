"""An OpenAI-compatible chat endpoint for the canary preflight (#13): a test double, not data.

Runs as a pod in the sandbox (python image, this file in a ConfigMap). Each call answers the
next scripted case, and every answer carries a canary that must never reach an export:

  1. a schema-valid triage decision for oom-restart, the canary in its reasoning (parse_ok true)
  2. text that is not JSON, the canary in it (parse_ok false: no object)
  3. HTTP 400 whose error message is the canary (http.response.status_code 400)

After the script, it repeats the last case. GET /calls returns how many calls it served.
"""

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CANARY = sys.argv[1] if len(sys.argv) > 1 else "CANARY-STUB-0000"


class Stub(BaseHTTPRequestHandler):
    calls = 0

    def log_message(self, format: str, *args: object) -> None:
        pass

    def do_GET(self) -> None:
        self._reply(200, {"calls": type(self).calls})

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        cls = type(self)
        cls.calls += 1
        case = min(cls.calls, 3)
        if case == 3:
            error = {"message": f"bad request {CANARY}", "type": "invalid_request_error"}
            self._reply(400, {"error": error})
            return
        if case == 1:
            decision = {"apply": True, "runbookId": "oom-restart", "reasoning": f"{CANARY} oom"}
            content = json.dumps(decision)
        else:
            content = f"not json {CANARY}"
        self._reply(
            200,
            {
                "id": f"chatcmpl-{cls.calls}",
                "object": "chat.completion",
                "created": 1,
                "model": "stub",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": content},
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            },
        )

    def _reply(self, status: int, payload: object) -> None:
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8080), Stub).serve_forever()  # noqa: S104 - in its pod
