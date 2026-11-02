/**
 * aegis.output.parse_ok for Vercel AI SDK calls (ADR-0006 section 6).
 *
 * The AI SDK validates generateObject's output after the HTTP attempt's span
 * (ai.generateObject.doGenerate, kind llm) has ended, and reports a failure on the outer span
 * (ai.generateObject, kind chain). So this processor parks each ended attempt under its parent
 * and decides the value when the parent ends, before either reaches the next processor: a
 * batch boundary between them cannot change the value. The value goes into FeatureValues, which
 * the sanitizing exporter reads; the ended span itself is never mutated.
 */
import { type Context, SpanStatusCode, diag } from "@opentelemetry/api";
import type { ReadableSpan, Span, SpanProcessor } from "@opentelemetry/sdk-trace-base";
import { OPERATION_ID, PARSE_OK } from "./attributes.js";

export const DO_GENERATE = "ai.generateObject.doGenerate";
export const GENERATE = "ai.generateObject";
export const NO_OBJECT = "AI_NoObjectGeneratedError";

/** Why an LLM span left the hold without a value. Counts only, never ids or content. */
export interface DegradedCounts {
  cap: number;
  parent_timeout: number;
  flush: number;
}

type Features = Record<string, boolean>;

export function spanKey(span: ReadableSpan): string {
  const ctx = span.spanContext();
  return `${ctx.traceId}-${ctx.spanId}`;
}

/** Feature values by span, read once by the exporter; bounded, oldest first out. */
export class FeatureValues {
  private readonly values = new Map<string, Features>();

  constructor(private readonly max: number) {}

  set(span: ReadableSpan, features: Features): void {
    this.values.set(spanKey(span), features);
    while (this.values.size > this.max) {
      const oldest = this.values.keys().next().value as string;
      this.values.delete(oldest);
    }
  }

  take(span: ReadableSpan): Features | undefined {
    const key = spanKey(span);
    const features = this.values.get(key);
    this.values.delete(key);
    return features;
  }
}

function exceptionType(span: ReadableSpan): unknown {
  return span.events.find((e) => e.name === "exception")?.attributes?.["exception.type"];
}

/** The ADR-0006 section 6 rule for one attempt under its finished parent. */
export function parseOk(attempt: ReadableSpan, parent: ReadableSpan): boolean | undefined {
  if (attempt.status.code === SpanStatusCode.ERROR) {
    // No response at all ends the attempt itself with AI_NoObjectGeneratedError.
    return exceptionType(attempt) === NO_OBJECT ? false : undefined;
  }
  if (parent.status.code !== SpanStatusCode.ERROR) return true;
  return exceptionType(parent) === NO_OBJECT ? false : undefined;
}

interface Parked {
  readonly span: ReadableSpan;
  readonly at: number;
}

export interface FeatureProcessorOptions {
  readonly values: FeatureValues;
  readonly maxHeld: number;
  readonly holdMs: number;
  /** Flushed after the hold is released, in this order (the batch behind `next`). */
  readonly flushAfter?: readonly SpanProcessor[];
  readonly now?: () => number;
}

export class FeatureProcessor implements SpanProcessor {
  private readonly parked = new Map<string, Parked[]>();
  private held = 0;
  private readonly counts: DegradedCounts = { cap: 0, parent_timeout: 0, flush: 0 };
  private readonly timer: NodeJS.Timeout;
  private readonly now: () => number;

  constructor(
    private readonly next: SpanProcessor,
    private readonly options: FeatureProcessorOptions,
  ) {
    this.now = options.now ?? Date.now;
    this.timer = setInterval(() => {
      this.sweep();
    }, Math.min(options.holdMs, 1000));
    this.timer.unref();
  }

  degraded(): DegradedCounts {
    return { ...this.counts };
  }

  onStart(span: Span, parentContext: Context): void {
    this.next.onStart(span, parentContext);
  }

  onEnd(span: ReadableSpan): void {
    this.sweep();
    const operation = span.attributes[OPERATION_ID];
    const parentId = span.parentSpanContext?.spanId;
    if (operation === DO_GENERATE && parentId !== undefined) {
      const group = this.parked.get(parentId) ?? [];
      group.push({ span, at: this.now() });
      this.parked.set(parentId, group);
      this.held++;
      while (this.held > this.options.maxHeld) this.releaseOldest("cap");
      return;
    }
    if (operation === GENERATE) {
      const group = this.parked.get(span.spanContext().spanId);
      if (group !== undefined) {
        this.parked.delete(span.spanContext().spanId);
        this.held -= group.length;
        for (const { span: attempt } of group) {
          const ok = parseOk(attempt, span);
          if (ok !== undefined) this.options.values.set(attempt, { [PARSE_OK]: ok });
          this.next.onEnd(attempt);
        }
      }
    }
    this.next.onEnd(span);
  }

  async forceFlush(): Promise<void> {
    this.releaseAll();
    await this.next.forceFlush();
    for (const p of this.options.flushAfter ?? []) await p.forceFlush();
  }

  async shutdown(): Promise<void> {
    clearInterval(this.timer);
    this.releaseAll();
    const { cap, parent_timeout, flush } = this.counts;
    if (cap + parent_timeout + flush > 0) {
      diag.warn(
        `traceblame-ts: LLM spans exported without aegis.output.parse_ok: cap=${String(cap)} ` +
          `parent_timeout=${String(parent_timeout)} flush=${String(flush)}`,
      );
    }
    await this.next.shutdown();
    for (const p of this.options.flushAfter ?? []) await p.shutdown();
  }

  private releaseGroup(parentId: string, reason: keyof DegradedCounts): void {
    const group = this.parked.get(parentId) ?? [];
    this.parked.delete(parentId);
    this.held -= group.length;
    this.counts[reason] += group.length;
    for (const { span } of group) this.next.onEnd(span);
  }

  private releaseOldest(reason: keyof DegradedCounts): void {
    // Map order is insertion order: the first parent was parked longest ago.
    const first = this.parked.keys().next();
    if (first.done !== true) this.releaseGroup(first.value, reason);
  }

  private releaseAll(): void {
    for (const parentId of [...this.parked.keys()]) this.releaseGroup(parentId, "flush");
  }

  private sweep(): void {
    const limit = this.now() - this.options.holdMs;
    for (const [parentId, group] of [...this.parked]) {
      if ((group[0]?.at ?? Infinity) <= limit) this.releaseGroup(parentId, "parent_timeout");
    }
  }
}
