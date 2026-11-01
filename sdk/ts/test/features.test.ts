// aegis.output.parse_ok on AI SDK calls (ADR-0006 section 6), on the EXPORTED spans.
import { SpanStatusCode, diag, trace } from "@opentelemetry/api";
import type { ReadableSpan, SpanProcessor } from "@opentelemetry/sdk-trace-base";
import { APICallError, generateObject } from "ai";
import { MockLanguageModelV1 } from "ai/test";
import { createOpenAI } from "@ai-sdk/openai";
import { describe, expect, it } from "vitest";
import { z } from "zod";
import { DO_GENERATE, FeatureProcessor, FeatureValues, GENERATE, NO_OBJECT } from "../src/features.js";
import { AI_SDK_TELEMETRY } from "../src/provider.js";
import { BATCHING, byOperation, completion, exported, only, openaiFetch } from "./helpers.js";

const PARSE_OK = "aegis.output.parse_ok";
const schema = z.object({ apply: z.boolean(), runbookId: z.string().nullable() });

function mock(text: string | undefined): MockLanguageModelV1 {
  return new MockLanguageModelV1({
    defaultObjectGenerationMode: "json",
    supportsStructuredOutputs: true,
    doGenerate: () =>
      Promise.resolve({
        rawCall: { rawPrompt: null, rawSettings: {} },
        finishReason: "stop",
        usage: { promptTokens: 1, completionTokens: 1 },
        text,
      }),
  });
}

async function call(model: MockLanguageModelV1 | ReturnType<ReturnType<typeof createOpenAI>>, maxRetries = 0) {
  try {
    await generateObject({ model, schema, prompt: "p", maxRetries, experimental_telemetry: AI_SDK_TELEMETRY });
  } catch {
    // The app catches; the spans are what matters here.
  }
}

describe.each(BATCHING)("parse_ok with %s", (_name, batch) => {
  it.each([
    ["unparsable output", "{not json", false],
    ["schema mismatch", '{"apply": 1, "runbookId": null}', false],
    ["valid output", '{"apply": true, "runbookId": "oom-restart"}', true],
  ])("%s", async (_case, text, want) => {
    const { spans } = await exported(() => call(mock(text)), { batch });
    const llm = only(spans, DO_GENERATE);
    const chain = only(spans, GENERATE);
    expect(llm.attributes[PARSE_OK]).toBe(want);
    expect(chain.attributes).not.toHaveProperty(PARSE_OK);
    expect(llm.status.code).toBe(SpanStatusCode.UNSET);
    expect(chain.status.code).toBe(want ? SpanStatusCode.UNSET : SpanStatusCode.ERROR);
  });

  it("no response: false on the failed attempt itself", async () => {
    const { spans } = await exported(() => call(mock(undefined)), { batch });
    const llm = only(spans, DO_GENERATE);
    expect(llm.status.code).toBe(SpanStatusCode.ERROR);
    expect(llm.attributes[PARSE_OK]).toBe(false);
    expect(only(spans, GENERATE).attributes).not.toHaveProperty(PARSE_OK);
  });

  it("an API error: no value", async () => {
    const model = new MockLanguageModelV1({
      defaultObjectGenerationMode: "json",
      doGenerate: () =>
        Promise.reject(new APICallError({ message: "m", url: "u", requestBodyValues: {}, statusCode: 500, isRetryable: false })),
    });
    const { spans } = await exported(() => call(model), { batch });
    expect(only(spans, DO_GENERATE).attributes).not.toHaveProperty(PARSE_OK);
  });

  it("the hold keeps OpenLLMetry-js's transformation: name, provider, usage", async () => {
    const openai = createOpenAI({ apiKey: "k", fetch: openaiFetch([completion('{"apply": false, "runbookId": null}')]) });
    const { spans } = await exported(() => call(openai("test-model", { structuredOutputs: true })), { batch });
    const llm = only(spans, DO_GENERATE);
    expect(llm.name).toBe("chat test-model");
    expect(llm.attributes).toMatchObject({
      "gen_ai.provider.name": "openai",
      "gen_ai.usage.input_tokens": 10,
      "gen_ai.usage.output_tokens": 5,
      "gen_ai.usage.total_tokens": 15,
      "ai.sdk.version": "4.3.19",
      [PARSE_OK]: true,
    });
    expect(llm.attributes).not.toHaveProperty("ai.model.provider");
    expect(llm.attributes).not.toHaveProperty("ai.usage.promptTokens");
    // Export order is end order: the attempt before the call that contains it.
    expect(spans.map((s) => s.attributes["ai.operationId"])).toEqual([DO_GENERATE, GENERATE]);
    expect(only(spans, GENERATE).name).toBe("chat test-model");
  });
});

it("parse_ok follows each retry attempt: only the attempt that answered gets a value", async () => {
  let calls = 0;
  const model = new MockLanguageModelV1({
    defaultObjectGenerationMode: "json",
    supportsStructuredOutputs: true,
    doGenerate: () =>
      calls++ === 0
        ? Promise.reject(new APICallError({ message: "m", url: "u", requestBodyValues: {}, statusCode: 503, isRetryable: true }))
        : Promise.resolve({
            rawCall: { rawPrompt: null, rawSettings: {} },
            finishReason: "stop",
            usage: { promptTokens: 1, completionTokens: 1 },
            text: '{"apply": true, "runbookId": "x"}',
          }),
  });
  const { spans } = await exported(() => call(model, 1));
  const attempts = byOperation(spans, DO_GENERATE);
  expect(attempts.map((s) => s.attributes[PARSE_OK])).toEqual([undefined, true]);
});

describe("degradation is counted", () => {
  // Spans shaped like the AI SDK's, from its tracer name, to keep a parent open on purpose.
  function aiSpan(name: string, operation: string): ReturnType<ReturnType<typeof trace.getTracer>["startSpan"]> {
    return trace.getTracer("ai").startSpan(name, { attributes: { "ai.operationId": operation } });
  }

  it("eviction at the cap increments `cap` and exports the attempt without a value", async () => {
    const { spans, shim } = await exported(
      async (s) => {
        const tracer = trace.getTracer("ai");
        await tracer.startActiveSpan("p1", { attributes: { "ai.operationId": GENERATE } }, async (p1) => {
          aiSpan("c1", DO_GENERATE).end();
          await tracer.startActiveSpan("p2", { attributes: { "ai.operationId": GENERATE } }, (p2) => {
            aiSpan("c2", DO_GENERATE).end(); // two held, cap 1: c1 goes out
            expect(s.degraded()).toEqual({ cap: 1, parent_timeout: 0, flush: 0 });
            p2.end();
            return Promise.resolve();
          });
          p1.end();
        });
      },
      { config: { maxTraces: 1 } },
    );
    expect(shim.degraded().cap).toBe(1);
    const values = Object.fromEntries(byOperation(spans, DO_GENERATE).map((s) => [s.name, s.attributes[PARSE_OK]]));
    expect(values).toEqual({ c1: undefined, c2: true });
  });

  it("a parent still open at flush increments `flush`", async () => {
    const { shim } = await exported(async (s) => {
      await trace.getTracer("ai").startActiveSpan("p", { attributes: { "ai.operationId": GENERATE } }, async (p) => {
        aiSpan("c", DO_GENERATE).end();
        await s.forceFlush();
        p.end();
      });
    });
    expect(shim.degraded()).toEqual({ cap: 0, parent_timeout: 0, flush: 1 });
  });
});

describe("FeatureProcessor", () => {
  class Recorder implements SpanProcessor {
    ended: string[] = [];
    onStart(): void {}
    onEnd(span: ReadableSpan): void {
      this.ended.push(span.name);
    }
    forceFlush(): Promise<void> {
      return Promise.resolve();
    }
    shutdown(): Promise<void> {
      return Promise.resolve();
    }
  }

  let ids = 0;
  function span(name: string, operation: string | undefined, parent?: ReadableSpan, error?: string): ReadableSpan {
    const spanId = (++ids).toString(16).padStart(16, "0");
    return {
      name,
      attributes: operation === undefined ? {} : { "ai.operationId": operation },
      spanContext: () => ({ traceId: "1".repeat(32), spanId, traceFlags: 1 }),
      ...(parent === undefined ? {} : { parentSpanContext: parent.spanContext() }),
      status: { code: error === undefined ? SpanStatusCode.UNSET : SpanStatusCode.ERROR },
      events: error === undefined ? [] : [{ name: "exception", time: [0, 0], attributes: { "exception.type": error } }],
    } as unknown as ReadableSpan;
  }

  function setup(now: { t: number }, maxHeld = 10) {
    const next = new Recorder();
    const values = new FeatureValues(100);
    const fp = new FeatureProcessor(next, { values, maxHeld, holdMs: 100, now: () => now.t });
    return { next, values, fp };
  }

  it("a parent that never ends: `parent_timeout` after the hold limit", async () => {
    const now = { t: 0 };
    const { next, fp } = setup(now);
    const parent = span("p", GENERATE);
    fp.onEnd(span("c", DO_GENERATE, parent));
    expect(next.ended).toEqual([]);
    now.t = 150;
    fp.onEnd(span("other", undefined));
    expect(next.ended).toEqual(["c", "other"]);
    expect(fp.degraded()).toEqual({ cap: 0, parent_timeout: 1, flush: 0 });
    await fp.shutdown();
  });

  it("schema failure on the parent makes the held attempt false; the parent follows it", async () => {
    const { next, values, fp } = setup({ t: 0 });
    const parent = span("p", GENERATE, undefined, NO_OBJECT);
    const attempt = span("c", DO_GENERATE, parent);
    fp.onEnd(attempt);
    fp.onEnd(parent);
    expect(next.ended).toEqual(["c", "p"]);
    expect(values.take(attempt)).toEqual({ [PARSE_OK]: false });
    expect(values.take(parent)).toBeUndefined();
    await fp.shutdown();
  });

  it("logs the counts at shutdown, and nothing else", async () => {
    const lines: string[] = [];
    diag.setLogger({ warn: (m: string) => lines.push(m), error() {}, info() {}, debug() {}, verbose() {} });
    try {
      const { fp } = setup({ t: 0 }, 1);
      const p1 = span("p1", GENERATE);
      const p2 = span("p2", GENERATE);
      fp.onEnd(span("c1", DO_GENERATE, p1));
      fp.onEnd(span("c2", DO_GENERATE, p2));
      await fp.shutdown();
      expect(lines).toEqual(["traceblame-ts: LLM spans exported without aegis.output.parse_ok: cap=1 parent_timeout=0 flush=1"]);
    } finally {
      diag.disable();
    }
  });
});
