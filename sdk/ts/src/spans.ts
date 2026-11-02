/**
 * Agent and tool spans in OpenInference conventions, as traceblame-py emits them, for code that
 * no instrumentation covers. They carry names and hashes, never text (ADR-0006 section 2).
 */
import { type Attributes, type Span, SpanStatusCode, trace } from "@opentelemetry/api";
import { ARGS_HASH } from "./attributes.js";
import { argsHash } from "./argsHash.js";
import { VERSION } from "./version.js";

export const SCOPE = "traceblame_ts";
const KIND = "openinference.span.kind";

function tracer() {
  return trace.getTracer(SCOPE, VERSION);
}

function inSpan<T>(name: string, attributes: Attributes, fn: (span: Span) => T | Promise<T>): Promise<T> {
  return tracer().startActiveSpan(name, { attributes }, async (span) => {
    try {
      return await fn(span);
    } catch (error) {
      // Only the class survives export: the allowlist keeps exception.type alone.
      span.recordException(error instanceof Error ? error : String(error));
      span.setStatus({ code: SpanStatusCode.ERROR });
      throw error;
    } finally {
      span.end();
    }
  });
}

/** An AGENT span named after the agent, active while fn runs. */
export function agentSpan<T>(name: string, fn: (span: Span) => T | Promise<T>): Promise<T> {
  return inSpan(name, { [KIND]: "AGENT", "agent.name": name }, fn);
}

export class Tool {
  constructor(readonly span: Span) {}

  /** The tool reported a failure without throwing. errorType is a class, not a message. */
  fail(errorType: string): void {
    this.span.setStatus({ code: SpanStatusCode.ERROR });
    this.span.setAttribute("error.type", errorType);
  }
}

/**
 * A TOOL span with aegis.tool.args_hash of the arguments; the arguments are not recorded.
 * Arguments that are not JSON-serializable get no hash.
 */
export function toolSpan<T>(name: string, args: string | object | undefined, fn: (tool: Tool) => T | Promise<T>): Promise<T> {
  const attributes: Attributes = { [KIND]: "TOOL", "tool.name": name };
  if (args !== undefined) {
    try {
      attributes[ARGS_HASH] = argsHash(args);
    } catch (error) {
      if (!(error instanceof TypeError)) throw error;
    }
  }
  return inSpan(name, attributes, (span) => fn(new Tool(span)));
}
