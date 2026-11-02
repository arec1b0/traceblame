/** Correlation ids and markers set on spans (ADR-0006 section 5). */
import { type Context, type Span as ApiSpan, context, createContextKey, trace } from "@opentelemetry/api";
import type { Span, SpanProcessor } from "@opentelemetry/sdk-trace-base";
import { BUNDLE_DIGEST, DECISION_ID, FAULT_INJECTION_ID, SESSION_ID, checked } from "./attributes.js";

const SESSION = createContextKey("traceblame-ts session");
const BUNDLE = createContextKey("traceblame-ts bundle");
const DECISION = createContextKey("traceblame-ts decision");

function scoped<T>(key: symbol, name: string, value: string, fn: () => T): T {
  return context.with(context.active().setValue(key, checked(name, value)), fn);
}

/** aegis.session.id on every span started inside fn. */
export function session<T>(sessionId: string, fn: () => T): T {
  return scoped(SESSION, SESSION_ID, sessionId, fn);
}

/** aegis.bundle.digest on every span started inside fn; overrides the configured one. */
export function bundle<T>(digest: string, fn: () => T): T {
  return scoped(BUNDLE, BUNDLE_DIGEST, digest, fn);
}

/** aegis.decision.id on every span started inside fn (spans that passed an AgentGate decision). */
export function decision<T>(decisionId: string, fn: () => T): T {
  return scoped(DECISION, DECISION_ID, decisionId, fn);
}

/** aegis.fault.injection.id on the span a fault is injected at (the active one by default). */
export function setFaultInjectionId(injectionId: string, span: ApiSpan | undefined = trace.getActiveSpan()): void {
  const value = checked(FAULT_INJECTION_ID, injectionId);
  if (span === undefined || !span.isRecording()) {
    throw new Error("setFaultInjectionId needs a recording span");
  }
  span.setAttribute(FAULT_INJECTION_ID, value);
}

/** Stamps the ids of the starting span's context on it. */
export class ContextProcessor implements SpanProcessor {
  constructor(private readonly bundleDigest?: string) {}

  onStart(span: Span, parentContext: Context): void {
    const values: [string, unknown][] = [
      [SESSION_ID, parentContext.getValue(SESSION)],
      [BUNDLE_DIGEST, parentContext.getValue(BUNDLE) ?? this.bundleDigest],
      [DECISION_ID, parentContext.getValue(DECISION)],
    ];
    for (const [name, value] of values) {
      if (typeof value === "string") span.setAttribute(name, value);
    }
  }

  onEnd(): void {
    // Nothing to do at end.
  }

  forceFlush(): Promise<void> {
    return Promise.resolve();
  }

  shutdown(): Promise<void> {
    return Promise.resolve();
  }
}
