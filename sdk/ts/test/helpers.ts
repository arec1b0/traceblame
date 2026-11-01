import type { BufferConfig, ReadableSpan } from "@opentelemetry/sdk-trace-base";
import { InMemorySpanExporter } from "@opentelemetry/sdk-trace-base";
import { type ShimConfig, shimConfig } from "../src/config.js";
import { type Shim, instrument } from "../src/provider.js";

export const BATCHING: [string, BufferConfig | undefined][] = [
  ["default batching", undefined],
  ["batch size 1", { maxExportBatchSize: 1, scheduledDelayMillis: 1 }],
];

/** Runs fn under a fresh shim and returns what reached the exporter behind it. */
export async function exported(
  fn: (shim: Shim) => Promise<unknown>,
  options: { config?: Partial<ShimConfig>; batch?: BufferConfig | undefined } = {},
): Promise<{ spans: ReadableSpan[]; shim: Shim }> {
  const exporter = new InMemorySpanExporter();
  const shim = instrument({
    serviceName: "traceblame-ts-test",
    serviceVersion: "0.0.0",
    config: shimConfig(options.config),
    exporter,
    ...(options.batch === undefined ? {} : { batch: options.batch }),
  });
  try {
    await fn(shim);
    await shim.forceFlush();
    return { spans: exporter.getFinishedSpans(), shim };
  } finally {
    await shim.shutdown();
  }
}

export function byOperation(spans: ReadableSpan[], operation: string): ReadableSpan[] {
  return spans.filter((s) => s.attributes["ai.operationId"] === operation);
}

export function only(spans: ReadableSpan[], operation: string): ReadableSpan {
  const found = byOperation(spans, operation);
  if (found.length !== 1) throw new Error(`want one ${operation} span, got ${String(found.length)}`);
  return found[0] as ReadableSpan;
}

/** Every string a span would put on the wire, for content checks. */
export function wireText(spans: ReadableSpan[]): string {
  return JSON.stringify(
    spans.map((s) => ({
      name: s.name,
      attributes: s.attributes,
      events: s.events,
      links: s.links,
      status: s.status,
      resource: s.resource.attributes,
    })),
  );
}

/** An OpenAI chat completions endpoint: one scripted response per call. */
export function openaiFetch(responses: { status: number; body: unknown }[]): typeof fetch {
  let call = 0;
  return () => {
    const r = responses[Math.min(call++, responses.length - 1)] as { status: number; body: unknown };
    return Promise.resolve(
      new Response(JSON.stringify(r.body), { status: r.status, headers: { "content-type": "application/json" } }),
    );
  };
}

export function completion(content: string): { status: number; body: unknown } {
  return {
    status: 200,
    body: {
      id: "chatcmpl-test",
      object: "chat.completion",
      created: 1,
      model: "test-model",
      choices: [{ index: 0, finish_reason: "stop", message: { role: "assistant", content } }],
      usage: { prompt_tokens: 10, completion_tokens: 5, total_tokens: 15 },
    },
  };
}
