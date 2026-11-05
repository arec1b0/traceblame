// traceblame-faults preload for runbook-agent (#14): model and prompt faults at the LLM seam.
//
// Exists only in the eval image that tools/faults/run_runbook_agent.py builds FROM the capture
// image (NODE_OPTIONS=--import <this file>); never in the app's own image or a non-eval
// manifest (tests/scripts/test_faults_preload_guard.py). It is a no-op unless
// TRACEBLAME_FAULTS=1 and TRACEBLAME_TENANT_ID is one of TRACEBLAME_EVAL_TENANTS, the same rule
// as traceblame_faults.guard.
//
// Before each task the driver writes <dir>/armed.json:
//   {task, query_ref, seed, kind, injection_id, params, system_text?}
// kind "" is a clean task. On every chat call the preload adds a request seed (as
// traceblame_faults.ids.request_seed). For prompt_variant it replaces the system message with
// system_text on every call; for malformed_output it rewrites the call_index-th response.
// The span marked (aegis.fault.injection.id, through traceblame-ts) is the active doGenerate
// span. One JSON line per call goes to <dir>/ledger.jsonl: ids, counts, digests, never text.
// The rewrites are the same as traceblame_faults.mutate's (vectors/mutations.json).

import { createHash } from "node:crypto";
import { appendFileSync, readFileSync, realpathSync } from "node:fs";
import { createRequire } from "node:module";
import { pathToFileURL } from "node:url";

export const APP = "runbook-agent";
const CHAT_PATH = "/chat/completions";
const FENCE = ["```json\n", "\n```"];
const PROSE = "Here is my next step:\n";
const DROP_HEADERS = new Set(["content-length", "content-encoding", "transfer-encoding"]);

export function evalTenants(value) {
  return value
    .trim()
    .replace(/^\[/, "")
    .replace(/\]$/, "")
    .split(",")
    .map((t) => t.trim())
    .filter((t) => t.length > 0);
}

export function enabled(env) {
  const tenant = env.TRACEBLAME_TENANT_ID ?? "";
  return (
    env.TRACEBLAME_FAULTS === "1" &&
    tenant.length > 0 &&
    evalTenants(env.TRACEBLAME_EVAL_TENANTS ?? "").includes(tenant)
  );
}

export function requestSeed(seed, queryRef, callIndex) {
  const hex = createHash("sha256").update(`${seed}:${queryRef}:${callIndex}`).digest("hex");
  return Number.parseInt(hex.slice(0, 8), 16) & 0x7fffffff;
}

function jcsString(s) {
  // JSON.stringify escapes exactly as RFC 8785 for strings.
  return JSON.stringify(s);
}

export function canonical(obj) {
  // RFC 8785 for objects whose leaves are strings; Array.sort compares UTF-16 code units.
  const parts = Object.keys(obj)
    .sort()
    .map((k) => {
      const v = obj[k];
      if (typeof v === "string") return `${jcsString(k)}:${jcsString(v)}`;
      if (v !== null && typeof v === "object" && !Array.isArray(v)) {
        return `${jcsString(k)}:${canonical(v)}`;
      }
      throw new TypeError(`canonical: ${k} is not a string or object`);
    });
  return `{${parts.join(",")}}`;
}

export function digest(app, templates) {
  const text = canonical({ app, templates });
  return `sha256:${createHash("sha256").update(text, "utf8").digest("hex")}`;
}

function obj(body) {
  const data = JSON.parse(body);
  if (data === null || typeof data !== "object" || Array.isArray(data)) {
    throw new Error("body is not a JSON object");
  }
  return data;
}

export function withFields(body, fields) {
  return JSON.stringify({ ...obj(body), ...fields });
}

function systemIndex(data) {
  if (!Array.isArray(data.messages)) return -1;
  return data.messages.findIndex((m) => m && m.role === "system");
}

export function systemText(body) {
  let data;
  try {
    data = obj(body);
  } catch {
    return null;
  }
  const i = systemIndex(data);
  if (i < 0) return null;
  const content = data.messages[i].content;
  return typeof content === "string" ? content : null;
}

export function withSystem(body, text) {
  const data = obj(body);
  const i = systemIndex(data);
  if (i < 0) throw new Error("request has no system message");
  const messages = data.messages.slice();
  messages[i] = { ...messages[i], content: text };
  return JSON.stringify({ ...data, messages });
}

export function parsesAsObject(text) {
  try {
    const v = JSON.parse(text);
    return v !== null && typeof v === "object" && !Array.isArray(v);
  } catch {
    return false;
  }
}

function truncate(text, cutFraction) {
  const points = Array.from(text); // code points, as Python's list(str)
  let cut = Math.max(1, Math.floor(points.length * cutFraction));
  let out = points.slice(0, cut).join("");
  while (cut > 0 && parsesAsObject(out)) {
    cut -= 1;
    out = points.slice(0, cut).join("");
  }
  return out;
}

export function malform(body, mode, cutFraction) {
  const data = obj(body);
  if (!Array.isArray(data.choices) || data.choices.length === 0) {
    throw new Error("response has no choices");
  }
  const first = data.choices[0];
  const text = first.message?.content;
  if (typeof text !== "string") throw new Error("first choice has no text content");
  let next;
  if (mode === "truncate") next = truncate(text, cutFraction);
  else if (mode === "fence") next = FENCE[0] + text + FENCE[1];
  else if (mode === "prose") next = PROSE + text;
  else throw new Error(`mode ${mode} is not one of truncate, fence, prose`);
  const choices = data.choices.slice();
  choices[0] = { ...first, message: { ...first.message, content: next } };
  return JSON.stringify({ ...data, choices });
}

function readArmed(dir) {
  try {
    return JSON.parse(readFileSync(`${dir}/armed.json`, "utf8"));
  } catch {
    return null;
  }
}

async function otel() {
  // The app's own traceblame-ts and the @opentelemetry/api it resolves (pnpm keeps it beside
  // the shim's real directory). The API's global registry makes the active span the same one
  // whichever copy of the module asks.
  const app = createRequire("/app/package.json");
  const shimPath = realpathSync(app.resolve("traceblame-ts"));
  const shim = await import(pathToFileURL(shimPath).href);
  const api = await import(pathToFileURL(createRequire(shimPath).resolve("@opentelemetry/api")).href);
  return { shim, api: api.default ?? api };
}

export function install(env, { fetchImpl = globalThis.fetch, load = otel, now = () => new Date() } = {}) {
  const dir = env.TRACEBLAME_FAULTS_DIR ?? "/tmp/faults";
  const calls = new Map();
  let loaded = null;
  const original = fetchImpl;

  async function handle(input, init) {
    const url = typeof input === "string" ? input : (input?.url ?? String(input));
    const method = (init?.method ?? "GET").toUpperCase();
    const armed = readArmed(dir);
    if (!armed || method !== "POST" || !url.endsWith(CHAT_PATH) || typeof init?.body !== "string") {
      return original(input, init);
    }
    const n = (calls.get(armed.task) ?? 0) + 1;
    calls.set(armed.task, n);
    loaded ??= await load();
    const span = loaded.api.trace.getActiveSpan();
    const line = {
      task: armed.task,
      query_ref: armed.query_ref,
      call_index: n,
      trace_id: span?.spanContext().traceId ?? null,
      kind: armed.kind,
      injection_id: armed.injection_id,
      applied: false,
      marker: null,
      at: now().toISOString(),
    };
    let body = init.body;
    const req = JSON.parse(body);
    line.request = {
      temperature: req.temperature ?? null,
      max_tokens: req.max_tokens ?? null,
      seed: requestSeed(armed.seed, armed.query_ref, n),
    };
    body = withFields(body, { seed: line.request.seed });
    const mark = () => {
      line.applied = true;
      try {
        loaded.shim.setFaultInjectionId(armed.injection_id);
        line.marker = "set";
      } catch (err) {
        line.marker = `error:${err?.name ?? "Error"}`;
      }
    };
    if (armed.kind === "prompt_variant") {
      body = withSystem(body, armed.system_text);
      mark();
    }
    const sent = systemText(body);
    line.sent_digest = sent === null ? null : digest(APP, { system: sent });
    const p = armed.params ?? {};
    const due =
      armed.kind === "malformed_output" &&
      (n === p.call_index || (p.persist === "all" && n >= p.call_index));
    let response = await original(input, { ...init, body });
    if (due && response.ok) {
      const text = await response.text();
      const headers = new Headers();
      response.headers.forEach((v, k) => {
        if (!DROP_HEADERS.has(k.toLowerCase())) headers.set(k, v);
      });
      response = new Response(malform(text, p.mode, p.cut_fraction), {
        status: response.status,
        statusText: response.statusText,
        headers,
      });
      mark();
    }
    line.status = response.status;
    appendFileSync(`${dir}/ledger.jsonl`, `${JSON.stringify(line)}\n`);
    return response;
  }

  return handle;
}

if (enabled(process.env)) {
  globalThis.fetch = install(process.env);
}
