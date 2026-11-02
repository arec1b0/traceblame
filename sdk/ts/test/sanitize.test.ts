// The positive allowlist on attributes, events, links, status and resource (ADR-0006 section 3).
import { SpanKind, SpanStatusCode } from "@opentelemetry/api";
import { ExportResultCode } from "@opentelemetry/core";
import { resourceFromAttributes } from "@opentelemetry/resources";
import { InMemorySpanExporter, type ReadableSpan } from "@opentelemetry/sdk-trace-base";
import { expect, it } from "vitest";
import { FeatureValues } from "../src/features.js";
import { SanitizingExporter, sanitize } from "../src/sanitize.js";

const ctx = { traceId: "1".repeat(32), spanId: "2".repeat(16), traceFlags: 1 };
const raw = {
  name: "chat m",
  kind: SpanKind.CLIENT,
  spanContext: () => ctx,
  parentSpanContext: { ...ctx, spanId: "3".repeat(16) },
  startTime: [1, 0],
  endTime: [2, 0],
  duration: [1, 0],
  ended: true,
  status: { code: SpanStatusCode.ERROR, message: "model 'secret' not found" },
  attributes: {
    "gen_ai.request.model": "m",
    "ai.operationId": "ai.generateObject.doGenerate",
    "ai.prompt.messages": "secret prompt",
    "ai.response.object": "secret output",
    "unknown.key": "x",
    "aegis.output.parse_ok": false,
  },
  events: [
    { name: "exception", time: [1, 5], attributes: { "exception.type": "AI_APICallError", "exception.message": "secret", "exception.stacktrace": "at /home/x" } },
    { name: "ai.stream.firstChunk", time: [1, 6], attributes: { text: "secret" } },
  ],
  links: [{ context: { ...ctx, spanId: "4".repeat(16) }, attributes: { "aegis.session.id": "s", note: "secret" } }],
  resource: resourceFromAttributes({ "service.name": "svc", "aegis.tenant.id": "t", "host.name": "laptop", "process.command_args": "secret" }),
  instrumentationScope: { name: "ai" },
  droppedAttributesCount: 0,
  droppedEventsCount: 0,
  droppedLinksCount: 0,
} as unknown as ReadableSpan;

it("keeps only allowlisted data, and the span's own value over a computed feature", () => {
  const out = sanitize(raw, { "aegis.output.parse_ok": true });
  expect(out.attributes).toEqual({
    "gen_ai.request.model": "m",
    "ai.operationId": "ai.generateObject.doGenerate",
    "aegis.output.parse_ok": false,
  });
  expect(out.status).toEqual({ code: SpanStatusCode.ERROR });
  expect(out.events).toEqual([{ name: "exception", time: [1, 5], attributes: { "exception.type": "AI_APICallError" } }]);
  expect(out.links).toEqual([{ context: { ...ctx, spanId: "4".repeat(16) }, attributes: { "aegis.session.id": "s" } }]);
  expect(out.resource.attributes).toEqual({ "service.name": "svc", "aegis.tenant.id": "t" });
  expect(out.spanContext()).toEqual(ctx);
  expect(out.parentSpanContext).toEqual(raw.parentSpanContext);
  expect(JSON.stringify(out)).not.toContain("secret");
});

it("adds computed features, once", async () => {
  const inner = new InMemorySpanExporter();
  const values = new FeatureValues(10);
  const plain = { ...raw, attributes: { "ai.operationId": "x" }, parentSpanContext: undefined } as unknown as ReadableSpan;
  values.set(plain, { "aegis.output.parse_ok": true });
  const exporter = new SanitizingExporter(inner, values);
  const code = await new Promise<number>((resolve) => {
    exporter.export([plain, plain], (r) => {
      resolve(r.code);
    });
  });
  expect(code).toBe(ExportResultCode.SUCCESS);
  expect(inner.getFinishedSpans().map((s) => s.attributes["aegis.output.parse_ok"])).toEqual([true, undefined]);
  expect(inner.getFinishedSpans()[0]?.parentSpanContext).toBeUndefined();
  await exporter.forceFlush();
  await exporter.shutdown();
});

it("feature values are bounded, oldest first out", () => {
  const values = new FeatureValues(1);
  const a = { spanContext: () => ({ ...ctx, spanId: "a".repeat(16) }) } as unknown as ReadableSpan;
  const b = { spanContext: () => ({ ...ctx, spanId: "b".repeat(16) }) } as unknown as ReadableSpan;
  values.set(a, { x: true });
  values.set(b, { x: false });
  expect(values.take(a)).toBeUndefined();
  expect(values.take(b)).toEqual({ x: false });
});
