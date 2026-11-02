/** Shim configuration, from arguments or the environment. */
import { BUNDLE_DIGEST, EVAL_RUN_ID, TENANT_ID, checked } from "./attributes.js";

export const DEFAULT_MAX_TRACES = 1024;
export const DEFAULT_HOLD_MS = 60_000;

/**
 * What the shim stamps on telemetry. Every value is checked against the profile.
 *
 * - tenantId: resource aegis.tenant.id. Behind an authenticating agent the agent replaces it
 *   with the authenticated tenant (ADR-0005).
 * - evalRunId: resource aegis.eval.run.id, for every trace of an eval run.
 * - bundleDigest: span aegis.bundle.digest on every span, when a promptops bundle is in use.
 * - maxTraces: LLM spans held at once for aegis.output.parse_ok (ADR-0006 section 6).
 * - holdMs: how long an LLM span waits for its parent before it is exported without a value.
 */
export interface ShimConfig {
  readonly tenantId?: string;
  readonly evalRunId?: string;
  readonly bundleDigest?: string;
  readonly maxTraces: number;
  readonly holdMs: number;
}

export function shimConfig(values: Partial<ShimConfig> = {}): ShimConfig {
  const config: ShimConfig = {
    maxTraces: DEFAULT_MAX_TRACES,
    holdMs: DEFAULT_HOLD_MS,
    ...values,
  };
  for (const [name, value] of [
    [TENANT_ID, config.tenantId],
    [EVAL_RUN_ID, config.evalRunId],
    [BUNDLE_DIGEST, config.bundleDigest],
  ] as const) {
    if (value !== undefined) checked(name, value);
  }
  if (!Number.isInteger(config.maxTraces) || config.maxTraces < 1) {
    throw new RangeError("maxTraces must be an integer of at least 1");
  }
  if (!Number.isInteger(config.holdMs) || config.holdMs < 1) {
    throw new RangeError("holdMs must be an integer of at least 1");
  }
  return Object.freeze(config);
}

/**
 * TRACEBLAME_TENANT_ID, TRACEBLAME_EVAL_RUN_ID, TRACEBLAME_BUNDLE_DIGEST, TRACEBLAME_MAX_TRACES,
 * TRACEBLAME_HOLD_MS. The exporter reads the standard OTEL_EXPORTER_OTLP_* variables.
 */
export function configFromEnv(env: NodeJS.ProcessEnv = process.env): ShimConfig {
  const values: { -readonly [K in keyof ShimConfig]?: ShimConfig[K] } = {};
  const set = (key: "tenantId" | "evalRunId" | "bundleDigest", name: string): void => {
    const value = env[name];
    if (value !== undefined && value !== "") values[key] = value;
  };
  set("tenantId", "TRACEBLAME_TENANT_ID");
  set("evalRunId", "TRACEBLAME_EVAL_RUN_ID");
  set("bundleDigest", "TRACEBLAME_BUNDLE_DIGEST");
  const int = (name: string): number | undefined => {
    const raw = env[name];
    if (raw === undefined || raw === "") return undefined;
    if (!/^[0-9]+$/.test(raw)) throw new RangeError(`${name} must be a positive integer`);
    return Number(raw);
  };
  const maxTraces = int("TRACEBLAME_MAX_TRACES");
  const holdMs = int("TRACEBLAME_HOLD_MS");
  if (maxTraces !== undefined) values.maxTraces = maxTraces;
  if (holdMs !== undefined) values.holdMs = holdMs;
  return shimConfig(values);
}
