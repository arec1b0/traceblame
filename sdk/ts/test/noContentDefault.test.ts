// Content off by default, through the real AI SDK and OpenAI provider (ADR-0006): canaries in
// every place the app's data goes reach none of the exported spans.
import { createOpenAI } from "@ai-sdk/openai";
import { generateObject, wrapLanguageModel } from "ai";
import { expect, it } from "vitest";
import { z } from "zod";
import { statusMiddleware } from "../src/middleware.js";
import { AI_SDK_TELEMETRY } from "../src/provider.js";
import { agentSpan, toolSpan } from "../src/spans.js";
import { session } from "../src/markers.js";
import { completion, exported, openaiFetch, wireText } from "./helpers.js";

const CANARY = {
  system: "CANARY-SYSTEM-1f2e",
  prompt: "CANARY-PROMPT-8a7b",
  schema: "CANARY-SCHEMA-3c4d",
  output: "CANARY-OUTPUT-5e6f",
  error: "CANARY-ERROR-9a0b",
  args: "CANARY-ARGS-7c8d",
  thrown: "CANARY-THROWN-2b3a",
};

it("no canary reaches the exporter", async () => {
  const openai = createOpenAI({
    apiKey: "k",
    fetch: openaiFetch([
      completion(JSON.stringify({ apply: true, runbookId: CANARY.output })),
      { status: 400, body: { error: { message: `bad request ${CANARY.error}`, type: "invalid_request_error" } } },
    ]),
  });
  const model = wrapLanguageModel({ model: openai("test-model", { structuredOutputs: true }), middleware: statusMiddleware });
  const schema = z.object({ apply: z.boolean(), runbookId: z.string().nullable() }).describe(CANARY.schema);
  const { spans } = await exported(() =>
    session("s-1", () =>
      agentSpan("runbook-agent", async () => {
        for (let i = 0; i < 2; i++) {
          try {
            await generateObject({ model, schema, system: CANARY.system, prompt: CANARY.prompt, maxRetries: 0, experimental_telemetry: AI_SDK_TELEMETRY });
          } catch {
            // The second call fails with the error body.
          }
        }
        await toolSpan("restart-service", { deployment: CANARY.args, namespace: "sandbox" }, (tool) => {
          tool.fail("rejected");
        });
        await toolSpan("collect-diagnostics", { deployment: "d" }, () => {
          throw new Error(CANARY.thrown);
        }).catch(() => undefined);
      }),
    ),
  );
  const text = wireText(spans);
  for (const canary of Object.values(CANARY)) expect(text).not.toContain(canary);
  // The spans are there, with their content-free facts.
  expect(spans.length).toBe(7);
  expect(text).toContain("aegis.tool.args_hash");
  expect(text).toContain('"aegis.output.parse_ok":true');
  expect(text).toContain('"http.response.status_code":400');
  expect(text).toContain('"exception.type":"Error"');
});
