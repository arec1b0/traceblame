/** instrument(): the shim's tracer provider and its single export path (ADR-0006). */
import { context, trace } from "@opentelemetry/api";
import { AsyncLocalStorageContextManager } from "@opentelemetry/context-async-hooks";
import { type ExportResult, ExportResultCode } from "@opentelemetry/core";
import { OTLPTraceExporter } from "@opentelemetry/exporter-trace-otlp-proto";
import { defaultResource, resourceFromAttributes } from "@opentelemetry/resources";
import {
  BatchSpanProcessor,
  type BufferConfig,
  type ReadableSpan,
  type SpanExporter,
} from "@opentelemetry/sdk-trace-base";
import { NodeTracerProvider } from "@opentelemetry/sdk-trace-node";
import { createSpanProcessor } from "@traceloop/node-server-sdk";
import { EVAL_RUN_ID, TENANT_ID } from "./attributes.js";
import { type ShimConfig, configFromEnv } from "./config.js";
import { type DegradedCounts, FeatureProcessor, FeatureValues } from "./features.js";
import { ContextProcessor } from "./markers.js";
import { SanitizingExporter } from "./sanitize.js";
import { SCOPE } from "./spans.js";

/** The shim cannot guarantee that it owns the only export path. */
export class ShimError extends Error {
  override name = "ShimError";
}

/**
 * The AI SDK telemetry settings to pass as `experimental_telemetry`: spans on, and no prompt,
 * schema or output recorded on them (ADR-0006 section 6).
 */
export const AI_SDK_TELEMETRY = Object.freeze({
  isEnabled: true,
  recordInputs: false,
  recordOutputs: false,
} as const);

/** Passes the spans OpenLLMetry-js has transformed on to the shim's batch processor. */
class HandOff implements SpanExporter {
  constructor(private readonly batch: BatchSpanProcessor) {}

  export(spans: ReadableSpan[], resultCallback: (result: ExportResult) => void): void {
    for (const span of spans) this.batch.onEnd(span);
    resultCallback({ code: ExportResultCode.SUCCESS });
  }

  shutdown(): Promise<void> {
    return Promise.resolve();
  }
}

export class Shim {
  private closed = false;

  constructor(
    readonly provider: NodeTracerProvider,
    private readonly features: FeatureProcessor,
  ) {}

  /** LLM spans exported without aegis.output.parse_ok, by reason (ADR-0006 section 6). */
  degraded(): DegradedCounts {
    return this.features.degraded();
  }

  forceFlush(): Promise<void> {
    return this.provider.forceFlush();
  }

  /** Flush and stop exporting; the global provider and context manager are released. */
  async shutdown(): Promise<void> {
    if (this.closed) return;
    this.closed = true;
    if (active === this) active = undefined;
    await this.provider.shutdown();
    trace.disable();
    context.disable();
  }
}

let active: Shim | undefined;

export function activeShim(): Shim | undefined {
  return active;
}

export interface InstrumentOptions {
  readonly serviceName: string;
  readonly serviceVersion?: string;
  readonly config?: ShimConfig;
  /** Default: OTLP/protobuf over HTTP, configured by OTEL_EXPORTER_OTLP_* (endpoint, headers). */
  readonly exporter?: SpanExporter;
  /** The batch processor's settings (tests use maxExportBatchSize 1). */
  readonly batch?: BufferConfig;
}

/**
 * Set up tracing for the process: the shim's provider becomes the global one. Throws ShimError
 * if a global tracer provider or context manager is already set.
 *
 * Pipeline (ADR-0006 section 6): context processor -> feature processor -> OpenLLMetry-js span
 * processor -> batch processor -> sanitizing exporter -> exporter. Only spans of the AI SDK
 * (scope `ai`) and of the shim's helpers reach the exporter.
 */
export function instrument(options: InstrumentOptions): Shim {
  const config = options.config ?? configFromEnv();
  const attributes: Record<string, string> = { "service.name": options.serviceName };
  if (options.serviceVersion !== undefined) attributes["service.version"] = options.serviceVersion;
  if (config.tenantId !== undefined) attributes[TENANT_ID] = config.tenantId;
  if (config.evalRunId !== undefined) attributes[EVAL_RUN_ID] = config.evalRunId;

  const sink = options.exporter ?? new OTLPTraceExporter();
  // Written by the feature processor, read once per span by the sanitizing exporter.
  const values = new FeatureValues(config.maxTraces * 4);
  const sanitizing = new SanitizingExporter(sink, values);
  const batch = new BatchSpanProcessor(sanitizing, options.batch);
  const transform = createSpanProcessor({
    exporter: new HandOff(batch),
    disableBatch: true,
    allowedInstrumentationLibraries: [SCOPE],
  });
  const features = new FeatureProcessor(transform, {
    values,
    maxHeld: config.maxTraces,
    holdMs: config.holdMs,
    flushAfter: [batch],
  });
  const provider = new NodeTracerProvider({
    resource: defaultResource().merge(resourceFromAttributes(attributes)),
    // OTel JS takes processors only here: nothing can be added later.
    spanProcessors: [new ContextProcessor(config.bundleDigest), features],
  });

  if (!context.setGlobalContextManager(new AsyncLocalStorageContextManager().enable())) {
    void provider.shutdown();
    throw new ShimError("a global context manager is already set; the shim needs its own");
  }
  if (!trace.setGlobalTracerProvider(provider)) {
    context.disable();
    void provider.shutdown();
    throw new ShimError("a global tracer provider is already set; the shim must own the only export path");
  }
  const shim = new Shim(provider, features);
  active = shim;
  return shim;
}
