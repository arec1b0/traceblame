// node --test: the preload's guard, its rewrites against the shared vectors, and its fetch
// wrapper with a fake fetch and a fake shim (test doubles, no network).

import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

import {
  canonical,
  digest,
  enabled,
  evalTenants,
  install,
  malform,
  parsesAsObject,
  requestSeed,
  systemText,
  withFields,
  withSystem,
} from "./faults-preload.mjs";

const HERE = dirname(fileURLToPath(import.meta.url));
const VECTORS = JSON.parse(readFileSync(join(HERE, "..", "vectors", "mutations.json"), "utf8"));
const ON = {
  TRACEBLAME_FAULTS: "1",
  TRACEBLAME_TENANT_ID: "tenant-eval",
  TRACEBLAME_EVAL_TENANTS: "[tenant-eval]",
};

test("eval tenants read the ConfigMap format", () => {
  assert.deepEqual(evalTenants("[tenant-eval, tenant-x]"), ["tenant-eval", "tenant-x"]);
  assert.deepEqual(evalTenants("a,b"), ["a", "b"]);
  assert.deepEqual(evalTenants("[]"), []);
});

test("the guard needs the switch and an eval tenant", () => {
  assert.equal(enabled(ON), true);
  for (const env of [
    {},
    { ...ON, TRACEBLAME_FAULTS: "true" },
    { ...ON, TRACEBLAME_TENANT_ID: "tenant-a" },
    { ...ON, TRACEBLAME_EVAL_TENANTS: "[]" },
    { TRACEBLAME_FAULTS: "1", TRACEBLAME_EVAL_TENANTS: "[tenant-eval]" },
  ]) {
    assert.equal(enabled(env), false, JSON.stringify(env));
  }
});

for (const v of VECTORS.with_fields) {
  test(`with_fields ${v.name}`, () => {
    assert.deepEqual(JSON.parse(withFields(JSON.stringify(v.body), v.fields)), v.want);
  });
}

for (const v of VECTORS.with_system) {
  test(`with_system ${v.name}`, () => {
    const out = withSystem(JSON.stringify(v.body), v.text);
    assert.deepEqual(JSON.parse(out), v.want);
    assert.equal(systemText(out), v.text);
  });
}

for (const v of VECTORS.malform) {
  test(`malform ${v.name}`, () => {
    const out = malform(JSON.stringify(v.body), v.mode, v.cut_fraction);
    assert.deepEqual(JSON.parse(out), v.want);
    assert.equal(parsesAsObject(JSON.parse(out).choices[0].message.content), false);
  });
}

for (const v of VECTORS.request_seed) {
  test(`request_seed ${JSON.stringify(v.args)}`, () => {
    assert.equal(requestSeed(...v.args), v.want);
  });
}

for (const v of VECTORS.digest) {
  test(`digest ${v.name}`, () => {
    assert.equal(digest(v.app, v.templates), v.want);
  });
}

test("canonical sorts keys and refuses non-strings", () => {
  assert.equal(canonical({ b: "1", a: { c: "x" } }), '{"a":{"c":"x"},"b":"1"}');
  assert.throws(() => canonical({ a: 1 }), TypeError);
});

test("errors", () => {
  assert.throws(() => withSystem('{"messages":[{"role":"user","content":"u"}]}', "t"), /system/);
  assert.throws(() => malform('{"choices":[]}', "truncate", 0.5), /choices/);
  assert.throws(() => malform(JSON.stringify(VECTORS.malform[0].body), "shout", 0.5), /mode/);
  assert.throws(() => withFields("[1]", { seed: 1 }), /object/);
  assert.equal(systemText("not json"), null);
  assert.equal(systemText("{}"), null);
});

function harness(armed, { refuse = false } = {}) {
  const dir = mkdtempSync(join(tmpdir(), "faults-preload-"));
  if (armed) writeFileSync(join(dir, "armed.json"), JSON.stringify(armed));
  const sent = [];
  const marked = [];
  const reply = VECTORS.malform[0].body;
  const fetchImpl = async (input, init) => {
    sent.push(init?.body);
    return new Response(JSON.stringify(reply), {
      status: 200,
      headers: { "content-type": "application/json", "content-length": "999" },
    });
  };
  const span = { spanContext: () => ({ traceId: "4bf92f3577b34da6a3ce929d0e0e4736" }) };
  const load = async () => ({
    shim: {
      setFaultInjectionId: (id) => {
        if (refuse) throw new RangeError("not recording");
        marked.push(id);
      },
    },
    api: { trace: { getActiveSpan: () => span } },
  });
  const handle = install(
    { ...ON, TRACEBLAME_FAULTS_DIR: dir },
    { fetchImpl, load, now: () => new Date("2026-11-06T10:00:00.000Z") },
  );
  const ledger = () =>
    readFileSync(join(dir, "ledger.jsonl"), "utf8")
      .trim()
      .split("\n")
      .map((l) => JSON.parse(l));
  return { handle, sent, marked, ledger };
}

const REQUEST = JSON.stringify({
  model: "qwen2.5:7b",
  messages: [
    { role: "system", content: "You are an SRE triage assistant." },
    { role: "user", content: "alert" },
  ],
  temperature: 0,
});
const URL_CHAT = "http://host.docker.internal:11434/v1/chat/completions";

test("a clean task gets the request seed and a ledger line, no marker", async () => {
  const h = harness({ task: "oom-1", query_ref: "oom-1@s7", seed: 7, kind: "", injection_id: "", params: {} });
  const res = await h.handle(URL_CHAT, { method: "POST", body: REQUEST });
  assert.equal(parsesAsObject(JSON.parse(await res.text()).choices[0].message.content), true);
  assert.equal(JSON.parse(h.sent[0]).seed, requestSeed(7, "oom-1@s7", 1));
  assert.deepEqual(h.marked, []);
  const [line] = h.ledger();
  assert.equal(line.trace_id, "4bf92f3577b34da6a3ce929d0e0e4736");
  assert.equal(line.applied, false);
  assert.deepEqual(line.request, { temperature: 0, max_tokens: null, seed: requestSeed(7, "oom-1@s7", 1) });
  assert.equal(line.sent_digest, digest("runbook-agent", { system: "You are an SRE triage assistant." }));
  assert.equal(JSON.stringify(line).includes("triage assistant"), false, "no text in the ledger");
});

test("malformed_output rewrites the response on its call and marks the span", async () => {
  const h = harness({
    task: "oom-2",
    query_ref: "oom-2@s7",
    seed: 7,
    kind: "malformed_output",
    injection_id: "run:malformed_output:abc",
    params: { mode: "truncate", cut_fraction: 0.5, call_index: 1, persist: "once" },
  });
  const res = await h.handle(URL_CHAT, { method: "POST", body: REQUEST });
  const content = JSON.parse(await res.text()).choices[0].message.content;
  assert.equal(parsesAsObject(content), false);
  assert.equal(res.headers.get("content-length"), null);
  assert.deepEqual(h.marked, ["run:malformed_output:abc"]);
  const second = await h.handle(URL_CHAT, { method: "POST", body: REQUEST });
  assert.equal(parsesAsObject(JSON.parse(await second.text()).choices[0].message.content), true);
  assert.deepEqual(
    h.ledger().map((l) => [l.call_index, l.applied, l.marker]),
    [
      [1, true, "set"],
      [2, false, null],
    ],
  );
});

test("prompt_variant replaces the system message on every call", async () => {
  const h = harness({
    task: "lat-1",
    query_ref: "latency-1@s7",
    seed: 7,
    kind: "prompt_variant",
    injection_id: "run:prompt_variant:abc",
    params: { variant: "no-decline-rule" },
    system_text: "Variant system prompt.",
  });
  await h.handle(URL_CHAT, { method: "POST", body: REQUEST });
  assert.equal(systemText(h.sent[0]), "Variant system prompt.");
  const [line] = h.ledger();
  assert.equal(line.sent_digest, digest("runbook-agent", { system: "Variant system prompt." }));
  assert.equal(line.applied, true);
  assert.deepEqual(h.marked, ["run:prompt_variant:abc"]);
});

test("a marker the shim refuses is recorded, not thrown", async () => {
  const h = harness(
    {
      task: "t",
      query_ref: "q",
      seed: 1,
      kind: "prompt_variant",
      injection_id: "id",
      params: {},
      system_text: "s",
    },
    { refuse: true },
  );
  const res = await h.handle(URL_CHAT, { method: "POST", body: REQUEST });
  assert.equal(res.status, 200);
  const [line] = h.ledger();
  assert.equal(line.applied, true);
  assert.equal(line.marker, "error:RangeError");
});

test("anything but an armed chat call passes through untouched", async () => {
  const h = harness(null);
  await h.handle(URL_CHAT, { method: "POST", body: REQUEST });
  assert.equal(h.sent[0], REQUEST);
  const armed = harness({ task: "t", query_ref: "q", seed: 1, kind: "", injection_id: "", params: {} });
  await armed.handle("http://x/v1/models", { method: "GET" });
  assert.equal(armed.sent[0], undefined);
});
