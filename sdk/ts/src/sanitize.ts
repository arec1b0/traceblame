/** The exporter wrapper that rebuilds spans from the positive allowlist (ADR-0006 section 3). */
import type { Attributes } from "@opentelemetry/api";
import type { ExportResult } from "@opentelemetry/core";
import { resourceFromAttributes } from "@opentelemetry/resources";
import type { ReadableSpan, SpanExporter } from "@opentelemetry/sdk-trace-base";
import contract from "./contract.json" with { type: "json" };
import type { FeatureValues } from "./features.js";

const SPAN_KEYS = new Set(contract.span_keys);
const RESOURCE_KEYS = new Set(contract.resource_keys);
const EXCEPTION_KEYS = new Set(contract.exception_keys);
const EXCEPTION = "exception";

export function keepSpanKey(key: string): boolean {
  return SPAN_KEYS.has(key);
}

export function keepResourceKey(key: string): boolean {
  return RESOURCE_KEYS.has(key);
}

function pick(attributes: Attributes | undefined, keep: (key: string) => boolean): Attributes {
  const out: Attributes = {};
  for (const [key, value] of Object.entries(attributes ?? {})) {
    if (keep(key)) out[key] = value;
  }
  return out;
}

/**
 * A copy of the span with only allowlisted data, plus its features. The status keeps its code:
 * AI SDK errors carry the server's text and no status prefix, so the description goes.
 */
export function sanitize(span: ReadableSpan, features?: Attributes): ReadableSpan {
  const attributes = { ...features, ...pick(span.attributes, keepSpanKey) };
  const context = span.spanContext();
  return {
    name: span.name,
    kind: span.kind,
    spanContext: () => context,
    ...(span.parentSpanContext === undefined ? {} : { parentSpanContext: span.parentSpanContext }),
    startTime: span.startTime,
    endTime: span.endTime,
    duration: span.duration,
    ended: span.ended,
    status: { code: span.status.code },
    attributes,
    links: span.links.map((link) => ({ context: link.context, attributes: pick(link.attributes, keepSpanKey) })),
    events: span.events
      .filter((event) => event.name === EXCEPTION)
      .map((event) => ({
        name: EXCEPTION,
        time: event.time,
        attributes: pick(event.attributes, (key) => EXCEPTION_KEYS.has(key)),
      })),
    resource: resourceFromAttributes(pick(span.resource.attributes, keepResourceKey)),
    instrumentationScope: span.instrumentationScope,
    droppedAttributesCount: span.droppedAttributesCount,
    droppedEventsCount: span.droppedEventsCount,
    droppedLinksCount: span.droppedLinksCount,
  };
}

/** Hands the inner exporter only sanitized copies. */
export class SanitizingExporter implements SpanExporter {
  constructor(
    private readonly inner: SpanExporter,
    private readonly values: FeatureValues,
  ) {}

  export(spans: ReadableSpan[], resultCallback: (result: ExportResult) => void): void {
    this.inner.export(
      spans.map((span) => sanitize(span, this.values.take(span))),
      resultCallback,
    );
  }

  shutdown(): Promise<void> {
    return this.inner.shutdown();
  }

  forceFlush(): Promise<void> {
    return this.inner.forceFlush?.() ?? Promise.resolve();
  }
}
