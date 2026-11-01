/**
 * traceblame-ts: the TraceBlame shim for Vercel AI SDK applications (ADR-0006).
 *
 *   const shim = instrument({ serviceName: "runbook-agent" });
 *   const model = wrapLanguageModel({ model: openai("gpt-4o"), middleware: statusMiddleware });
 *   await session(sessionId, () => agentSpan("runbook-agent", () =>
 *     generateObject({ model, schema, prompt, experimental_telemetry: AI_SDK_TELEMETRY })));
 *   await shim.shutdown();
 */
export { ALGORITHM_VERSION, argsHash } from "./argsHash.js";
export { type ShimConfig, configFromEnv, shimConfig } from "./config.js";
export type { DegradedCounts } from "./features.js";
export { NotIJSONError, canonicalize } from "./jcs.js";
export { bundle, decision, session, setFaultInjectionId } from "./markers.js";
export { statusMiddleware } from "./middleware.js";
export { AI_SDK_TELEMETRY, type InstrumentOptions, Shim, ShimError, activeShim, instrument } from "./provider.js";
export { SCOPE, Tool, agentSpan, toolSpan } from "./spans.js";
