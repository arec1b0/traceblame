// Markers and correlation ids in the scopes the profile gives them (ADR-0006 section 5).
import { trace } from "@opentelemetry/api";
import { describe, expect, it } from "vitest";
import { configFromEnv, shimConfig } from "../src/config.js";
import { bundle, decision, session, setFaultInjectionId } from "../src/markers.js";
import { agentSpan, toolSpan } from "../src/spans.js";
import { exported } from "./helpers.js";

const DIGEST = "sha256:" + "a".repeat(64);
const OTHER = "sha256:" + "b".repeat(64);

describe("resource scope", () => {
  it("tenant and eval run id are on the resource, never on spans", async () => {
    const { spans } = await exported(() => agentSpan("a", () => undefined), {
      config: { tenantId: "tenant-eval", evalRunId: "rb-20261101-s7-abc1234" },
    });
    const span = spans[0];
    expect(span?.resource.attributes).toMatchObject({ "aegis.tenant.id": "tenant-eval", "aegis.eval.run.id": "rb-20261101-s7-abc1234" });
    expect(span?.attributes).not.toHaveProperty("aegis.tenant.id");
    expect(span?.attributes).not.toHaveProperty("aegis.eval.run.id");
  });
});

describe("span scope", () => {
  it("session, bundle and decision only inside their context", async () => {
    const { spans } = await exported(async () => {
      await agentSpan("outside", () => undefined);
      await session("s-1", () => bundle(DIGEST, () => decision("d-1", () => agentSpan("inside", () => undefined))));
    });
    const [outside, inside] = spans;
    expect(outside?.attributes).not.toHaveProperty("aegis.session.id");
    expect(outside?.attributes).not.toHaveProperty("aegis.bundle.digest");
    expect(inside?.attributes).toMatchObject({ "aegis.session.id": "s-1", "aegis.bundle.digest": DIGEST, "aegis.decision.id": "d-1" });
  });

  it("the configured bundle digest is the default; a scoped one overrides it", async () => {
    const { spans } = await exported(
      async () => {
        await agentSpan("default", () => undefined);
        await bundle(OTHER, () => agentSpan("scoped", () => undefined));
      },
      { config: { bundleDigest: DIGEST } },
    );
    expect(spans.map((s) => s.attributes["aegis.bundle.digest"])).toEqual([DIGEST, OTHER]);
  });

  it("the fault injection id is only on the span it was set on", async () => {
    const { spans } = await exported(() =>
      agentSpan("parent", () =>
        toolSpan("faulty", undefined, () => {
          setFaultInjectionId("fi-1");
        }),
      ),
    );
    const byName = Object.fromEntries(spans.map((s) => [s.name, s.attributes["aegis.fault.injection.id"]]));
    expect(byName).toEqual({ faulty: "fi-1", parent: undefined });
  });

  it("setting a fault id needs a recording span", () => {
    expect(() => {
      setFaultInjectionId("fi-1", trace.wrapSpanContext({ traceId: "1".repeat(32), spanId: "1".repeat(16), traceFlags: 1 }));
    }).toThrow("recording span");
    expect(() => {
      setFaultInjectionId("fi-1");
    }).toThrow("recording span");
  });
});

describe("patterns", () => {
  it.each([
    ["an eval run id with a space", { evalRunId: "rb 1" }],
    ["an eval run id over 128 characters", { evalRunId: "a".repeat(129) }],
    ["a digest without its prefix", { bundleDigest: "a".repeat(64) }],
    ["a digest with a trailing newline", { bundleDigest: DIGEST + "\n" }],
    ["an empty tenant", { tenantId: "" }],
  ])("refuses %s", (_name, values) => {
    expect(() => shimConfig(values)).toThrow();
  });

  it("refuses bad scoped values at the call", () => {
    expect(() => {
      bundle("sha256:x", () => undefined);
    }).toThrow(RangeError);
    expect(() => {
      session("", () => undefined);
    }).toThrow(TypeError);
    expect(() => {
      setFaultInjectionId("bad id");
    }).toThrow(RangeError);
  });

  it.each([
    ["maxTraces", { maxTraces: 0 }],
    ["holdMs", { holdMs: 1.5 }],
  ])("refuses a bad %s", (_name, values) => {
    expect(() => shimConfig(values)).toThrow(RangeError);
  });
});

describe("configFromEnv", () => {
  it("reads every variable", () => {
    expect(
      configFromEnv({
        TRACEBLAME_TENANT_ID: "tenant-a",
        TRACEBLAME_EVAL_RUN_ID: "run-1",
        TRACEBLAME_BUNDLE_DIGEST: DIGEST,
        TRACEBLAME_MAX_TRACES: "16",
        TRACEBLAME_HOLD_MS: "500",
      }),
    ).toEqual({ tenantId: "tenant-a", evalRunId: "run-1", bundleDigest: DIGEST, maxTraces: 16, holdMs: 500 });
  });

  it("uses the defaults for unset or empty variables", () => {
    expect(configFromEnv({ TRACEBLAME_TENANT_ID: "" })).toEqual({ maxTraces: 1024, holdMs: 60000 });
  });

  it("refuses a number that is not a positive integer", () => {
    expect(() => configFromEnv({ TRACEBLAME_MAX_TRACES: "-1" })).toThrow(RangeError);
  });
});
