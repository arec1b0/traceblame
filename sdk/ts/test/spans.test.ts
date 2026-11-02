// The helper spans: OpenInference conventions, ids and hashes only (ADR-0006 section 2).
import { readFileSync } from "node:fs";
import { SpanStatusCode } from "@opentelemetry/api";
import { expect, it } from "vitest";
import { argsHash } from "../src/argsHash.js";
import { SCOPE, agentSpan, toolSpan } from "../src/spans.js";
import { VERSION } from "../src/version.js";
import { exported } from "./helpers.js";

it("an agent span", async () => {
  const { spans } = await exported(() => agentSpan("runbook-agent", () => 1));
  expect(spans[0]?.attributes).toEqual({ "openinference.span.kind": "AGENT", "agent.name": "runbook-agent" });
  expect(spans[0]?.instrumentationScope).toMatchObject({ name: SCOPE, version: VERSION });
});

it("a tool span hashes its arguments and never records them", async () => {
  const args = { deployment: "checkout-api", namespace: "prod" };
  const { spans } = await exported(() => toolSpan("restart-service", args, () => "ok"));
  expect(spans[0]?.attributes).toEqual({
    "openinference.span.kind": "TOOL",
    "tool.name": "restart-service",
    "aegis.tool.args_hash": argsHash(args),
  });
});

it("fail() marks an error with its class", async () => {
  const { spans } = await exported(() => toolSpan("scale", "{}", (tool) => {
    tool.fail("rejected");
  }));
  expect(spans[0]?.status.code).toBe(SpanStatusCode.ERROR);
  expect(spans[0]?.attributes["error.type"]).toBe("rejected");
});

it("a throw records the exception class and rethrows", async () => {
  let caught: unknown;
  const { spans } = await exported(async () => {
    await toolSpan("t", undefined, () => {
      throw new RangeError("details");
    }).catch((e: unknown) => (caught = e));
    await agentSpan("a", () => Promise.reject(new Error("x"))).catch(() => undefined);
    await agentSpan("b", () => Promise.reject(new Error("x"))).catch(() => undefined);
  });
  expect(caught).toBeInstanceOf(RangeError);
  expect(spans[0]?.status.code).toBe(SpanStatusCode.ERROR);
  expect(spans[0]?.events.map((e) => e.attributes)).toEqual([{ "exception.type": "RangeError" }]);
});

it("arguments that cannot be serialized get no hash", async () => {
  const { spans } = await exported(() => toolSpan("t", { m: new Map() }, () => undefined));
  expect(spans[0]?.attributes).not.toHaveProperty("aegis.tool.args_hash");
});

it("the scope version is the package version", () => {
  const pkg = JSON.parse(readFileSync(new URL("../package.json", import.meta.url), "utf8")) as { version: string };
  expect(VERSION).toBe(pkg.version);
});
