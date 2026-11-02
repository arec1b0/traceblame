// http.response.status_code from AI_APICallError.statusCode, on the attempt span only
// (ADR-0006 section 6). Never from message text.
import { createOpenAI } from "@ai-sdk/openai";
import { APICallError, RetryError, generateObject, wrapLanguageModel } from "ai";
import { expect, it } from "vitest";
import { z } from "zod";
import { DO_GENERATE, GENERATE } from "../src/features.js";
import { statusMiddleware } from "../src/middleware.js";
import { AI_SDK_TELEMETRY } from "../src/provider.js";
import { toolSpan } from "../src/spans.js";
import { byOperation, completion, exported, mockModel, only, openaiFetch } from "./helpers.js";

const STATUS = "http.response.status_code";
const schema = z.object({ a: z.boolean() });

function model(responses: { status: number; body: unknown }[]) {
  const openai = createOpenAI({ apiKey: "k", fetch: openaiFetch(responses) });
  return wrapLanguageModel({ model: openai("test-model", { structuredOutputs: true }), middleware: statusMiddleware });
}

async function call(m: Parameters<typeof generateObject>[0]["model"], maxRetries = 0, telemetry = true) {
  try {
    await generateObject({ model: m, schema, prompt: "p", maxRetries, ...(telemetry ? { experimental_telemetry: AI_SDK_TELEMETRY } : {}) });
  } catch {
    // The app catches.
  }
}

const notFound = { status: 404, body: { error: { message: "model 'x' not found", type: "invalid_request_error" } } };
const unavailable = { status: 503, body: { error: { message: "overloaded", type: "server_error" } } };

it("a 404 lands on the attempt span, not on the call", async () => {
  const { spans } = await exported(() => call(model([notFound])));
  expect(only(spans, DO_GENERATE).attributes[STATUS]).toBe(404);
  expect(only(spans, GENERATE).attributes).not.toHaveProperty(STATUS);
});

it("each retry attempt gets its own code", async () => {
  const { spans } = await exported(() => call(model([unavailable, unavailable]), 1));
  expect(byOperation(spans, DO_GENERATE).map((s) => s.attributes[STATUS])).toEqual([503, 503]);
  expect(only(spans, GENERATE).attributes).not.toHaveProperty(STATUS);
});

it("a success records no status", async () => {
  const { spans } = await exported(() => call(model([completion('{"a": true}')])));
  expect(only(spans, DO_GENERATE).attributes).not.toHaveProperty(STATUS);
});

it("reads RetryError.lastError when a provider throws one from inside the attempt", async () => {
  const lastError = new APICallError({ message: "m", url: "u", requestBodyValues: {}, statusCode: 429, isRetryable: true });
  const inner = mockModel({
    defaultObjectGenerationMode: "json",
    doGenerate: () => Promise.reject(new RetryError({ message: "m", reason: "maxRetriesExceeded", errors: [lastError] })),
  });
  const { spans } = await exported(() => call(wrapLanguageModel({ model: inner, middleware: statusMiddleware })));
  expect(only(spans, DO_GENERATE).attributes[STATUS]).toBe(429);
});

it.each([
  ["a status below 400", 302],
  ["no status", undefined],
])("ignores %s", async (_name, statusCode) => {
  const inner = mockModel({
    defaultObjectGenerationMode: "json",
    doGenerate: () =>
      Promise.reject(
        new APICallError({
          message: "Error code: 500",
          url: "u",
          requestBodyValues: {},
          ...(statusCode === undefined ? {} : { statusCode }),
          isRetryable: false,
        }),
      ),
  });
  const { spans } = await exported(() => call(wrapLanguageModel({ model: inner, middleware: statusMiddleware })));
  // Never from the message, even one that reads like the OpenAI client's prefix.
  expect(only(spans, DO_GENERATE).attributes).not.toHaveProperty(STATUS);
});

it("with AI SDK telemetry off, the app's own active span is left alone", async () => {
  const { spans } = await exported(() => toolSpan("t", undefined, () => call(model([notFound]), 0, false)));
  expect(spans).toHaveLength(1);
  expect(spans[0]?.attributes).not.toHaveProperty(STATUS);
});

it("streaming errors are recorded too", async () => {
  const inner = mockModel({
    doStream: () =>
      Promise.reject(new APICallError({ message: "m", url: "u", requestBodyValues: {}, statusCode: 401, isRetryable: false })),
  });
  const wrapped = wrapLanguageModel({ model: inner, middleware: statusMiddleware });
  const { spans } = await exported(async () => {
    const { streamText } = await import("ai");
    const result = streamText({ model: wrapped, prompt: "p", experimental_telemetry: AI_SDK_TELEMETRY, onError: () => {} });
    await result.consumeStream();
  });
  expect(byOperation(spans, "ai.streamText.doStream").map((s) => s.attributes[STATUS])).toEqual([401]);
});
