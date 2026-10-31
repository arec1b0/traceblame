# ADR-0005: Tenant identity at the first authenticated hop

- **Status:** Proposed
- **Date:** 2026-10-31
- **Issue:** #10 (design issue #8)
- **Decider:** Daniil Krizhanovskyi

## Context

Profile v1 requires `aegis.tenant.id` on every resource. Until #10, the normalizer only
filled a missing value from config, so any client could claim any tenant. The #10 comment
set a default: the gateway overrides the tenant from the authenticated receiver identity,
and a shim value is trusted only in single-tenant mode. That default does not fit the
two-tier topology:

- **The gateway cannot see the application's identity.** Its receiver authenticates the
  agents, not the applications.
- **The identity cannot be forwarded either.** In exporterhelper v0.164.0, "context set by
  Auth extensions is **not** propagated through the persistent queue". The agent's
  `file_storage` queue (design §1.1) sits between the app's request and the gateway.
- **The agent→gateway hop needs TLS anyway.** gRPC per-RPC credentials need transport
  security: `bearertokenauth` "requires transport layer security enabled on the exporter",
  and the loadbalancing exporter speaks OTLP/gRPC only.

## Decision

- **The agent, the first authenticated hop, names the tenant.**
  - The agent's OTLP receivers require `basicauth`, with one htpasswd user per tenant.
  - The normalizer runs with `tenant_source: auth`. It **overrides** `aegis.tenant.id`
    with the authenticated `username`, so a shim value never survives.
  - A request without authentication cannot reach the processor. If one did, the tenant
    would be dropped and counted in `traceblame_normalizer_invalid_attrs_total`.
  - This happens before the `file_storage` queue, so the tenant travels as data.
- **The gateway accepts only agents, and keeps their tenant.**
  - The gateway's OTLP receiver requires an mTLS client certificate of the agents' CA.
  - A NetworkPolicy admits only agent pods to the OTLP port.
  - The gateway's normalizer runs with `tenant_source: config` and no `tenant_id`, so it
    keeps the agent-stamped tenant.
- **Eval and fault-injection markers are a tenant-trust decision (#36).**
  - The gateway keeps every trace that carries `aegis.eval.run.id` or
    `aegis.fault.injection.id` at 100%. Any client that could set them could force full
    sampling and pollute the D1 ground truth.
  - The normalizer keeps a marker only for a tenant in `eval_tenants`, decided on the tenant
    this ADR establishes: the authenticated one on the agent, the agent-stamped one on the
    gateway. Any other marker, one in the wrong scope, or one that fails the profile pattern
    is dropped and counted in `traceblame_normalizer_eval_marker_rejected_total{tenant, attr,
    reason}` (`tenant_not_allowed`, `scope`, `pattern`). It is a
    security signal, so it is not lumped into `invalid_attrs_total`, and an id is never a
    label.
  - Both tiers check, with one list from one source (in kind, the `eval-tenants` ConfigMap
    read by both pods). The gateway's pass keeps what the agent accepted, and it rejects
    what a different list would, so a drift shows up as gateway-side rejections.
  - The list is empty by default: no tenant may mark traces until an operator says so.
- **Single-tenant mode** is `tenant_source: config` with `tenant_id`: the shim value is
  trusted and the config only fills a missing one, as before #10.
- **Credentials live in the cluster only.** `make kind-up` generates every credential (CA,
  certificates, htpasswd, ClickHouse password) as Secrets, and none is committed.

The #10 comment allowed an ADR to choose otherwise; this is that choice, because the
gateway-side override cannot be built on the given topology.

## Consequences

**Positive**
- A client cannot write as another tenant: the `chat_basic` smoke test shows a shim
  `tenant-b` stored as `tenant-a`.
- The bench report checks every stored span against its generator's credentials.
- The gateway trusts one thing, agent certificates, instead of every client.

**Negative and risks**
- **Credential rotation needs a restart for now.** Tenants are htpasswd entries, so adding
  one means rotating a Secret; basicauth re-reads its file only on restart.
- **App → agent is not encrypted yet.** It is plain gRPC inside the node, so the basic-auth
  password crosses the node network in clear. That hop needs TLS before any non-demo use.
- **The local CA is short-lived.** It is valid for 30 days, and its rotation is manual
  (re-run `make kind-up` after deleting the Secrets).
- **An allowlisted tenant can mark every trace.** `eval_tenants` decides who may force
  100% sampling, not how much. A per-tenant budget on marked traces is a follow-up.
- **The allowlist changes on restart.** Like the htpasswd, `eval_tenants` is read at start.
  `make kind-deploy` restarts both tiers together. Until both have restarted, the gateway
  drops markers that only the agent's new list allows, and counts them.
- **A compromised agent can claim any tenant.** Agents are trusted. Per-tenant agent pools
  are a follow-up.
