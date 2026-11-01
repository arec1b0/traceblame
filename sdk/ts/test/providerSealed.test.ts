// One export path, owned by the shim (ADR-0006 section 1).
import { context, trace } from "@opentelemetry/api";
import { AsyncLocalStorageContextManager } from "@opentelemetry/context-async-hooks";
import { NodeTracerProvider } from "@opentelemetry/sdk-trace-node";
import { afterEach, expect, it } from "vitest";
import { shimConfig } from "../src/config.js";
import { ShimError, activeShim, instrument } from "../src/provider.js";

afterEach(() => {
  trace.disable();
  context.disable();
});

const options = { serviceName: "t", config: shimConfig() };

it("refuses to start when a global tracer provider is set", () => {
  trace.setGlobalTracerProvider(new NodeTracerProvider());
  expect(() => instrument(options)).toThrow(ShimError);
  // It gave back the context manager it had taken.
  expect(context.setGlobalContextManager(new AsyncLocalStorageContextManager())).toBe(true);
});

it("refuses to start when a global context manager is set", () => {
  context.setGlobalContextManager(new AsyncLocalStorageContextManager());
  expect(() => instrument(options)).toThrow(ShimError);
});

it("refuses a second instrument() while the first runs, and allows one after shutdown", async () => {
  const shim = instrument(options);
  expect(activeShim()).toBe(shim);
  expect(() => instrument(options)).toThrow(ShimError);
  await shim.shutdown();
  await shim.shutdown();
  expect(activeShim()).toBeUndefined();
  const again = instrument(options);
  await again.shutdown();
});

it("has no way to add a processor", async () => {
  const shim = instrument(options);
  expect("addSpanProcessor" in shim.provider).toBe(false);
  await shim.shutdown();
});

it("reads its config from the environment by default", async () => {
  process.env["TRACEBLAME_TENANT_ID"] = "tenant-env";
  try {
    const shim = instrument({ serviceName: "t" });
    await shim.shutdown();
  } finally {
    delete process.env["TRACEBLAME_TENANT_ID"];
  }
});
