# TraceBlame design (extract from "Aegis Platform — Top-3 Design Docs", 2026-10-30)

Source: https://claude.ai/artifact/RZEw31ATsTtgoZFUEkMdzu


### Shared conventions
Область
Решение
Альтернатива (в ADR)
Telemetry contract
aegis-telemetry profile v1 поверх OTel GenAI semconv (pinned), свои атрибуты только в namespace aegis.*
OpenInference conventions как canonical
Correlation IDs
trace_id, aegis.session.id, aegis.tenant.id, aegis.bundle.digest, aegis.decision.id
—
Data plane
Go (collector processor, AgentGate, rollout controller, существующий gateway)
Rust
Control/eval plane
Python 3.12 + uv workspace, FastAPI
—
Analytics storage
ClickHouse
Tempo + Postgres
State storage
Postgres через CloudNativePG
managed Postgres
Artifacts
MinIO (S3 API, object lock) + OCI registry (Zot)
Harbor, GHCR
Messaging
NATS JetStream
Redpanda, Redis Streams
Deploy
kind локально, k3s/Talos для demo-кластера; Helm + Argo CD app-of-apps; OpenTofu для облака
Flux, Kustomize
Supply chain
cosign keyless, Syft SBOM, SLSA provenance, сканеры pinned по digest
Notation
Releases
Conventional Commits, release-please, semver
semantic-release

### Repository topology
Три сервисных репозитория (traceblame, agentgate, promptops) + umbrella aegis-platform. Umbrella держит contracts/ (profile v1, JSON Schemas, OpenAPI), deploy/ (app-of-apps), e2e/ (сквозной сценарий) и C4-диаграммы. Сервисы потребляют contracts как версионированный пакет, contract tests в CI каждого сервиса. Trade-off против monorepo: больше CI-обвязки, зато каждый репозиторий pinned на GitHub и читается отдельно.

### Сквозной demo-сценарий
- 
promptops выкатывает новый bundle runbook-agent@1.4.0 canary 5%.
- 
Runbook agent на новом промпте пытается kubectl delete ns — AgentGate требует approval, approver отклоняет.
- 
TraceBlame атрибутирует рост ошибок к prompt и к aegis.bundle.digest кандидата.
- 
Rollout controller видит breach guardrail и откатывает canary меньше чем за 2 минуты.
- 
Всё видно в одном трейсе, одном audit trail и одной Grafana-папке.

## 1. TraceBlame
TraceBlame превращает OTel GenAI-трейсы в вердикт «кто виноват» (retrieval | prompt | tool | model | orchestration | unknown) с confidence и evidence, а ground truth для валидации берёт из собственного fault injector. Цель MVP: top-1 accuracy ≥ 0.80 на single-fault трейсах при overhead ≤ 3% p95.

### 1.1 System design
_[diagram: TraceBlame pipeline · 8 stages — see original doc]_

Верхний ряд — ingest и хранение, нижний — анализ; replay запускается только когда attributor не уверен.
**Компоненты**
#
Component
Lang
Ответственность
1
traceblame-py / traceblame-ts shims
Python, TS
Обёртки над OpenInference/OpenLLMetry instrumentation; добавляют aegis.* атрибуты; content capture выключен по умолчанию
2
normalizer processor
Go (ocb)
Маппинг vendor-атрибутов (llm.model_name, openinference.span.kind) в profile v1; redaction/hashing content; счётчик unmapped attrs
3
Collector agent tier
OTel Collector
DaemonSet; loadbalancing exporter роутит по trace_id, file_storage sending queue
4
Collector gateway tier
OTel Collector
tail_sampling: 100% ошибок, latency > p95, low eval score, 10% остального; export в ClickHouse + NATS
5
trace-assembler
Go
Считает трейс завершённым (root span end + grace 30 s), пишет trace_summary, публикует trace.completed
6
detectors
Python worker
Plugin-детекторы на span/trace уровне → findings; KEDA-скейлинг по lag NATS
7
attributor
Python
Взвешенная агрегация findings → blame; веса калибруются на injection set, версия в каждой записи
8
replayer
Python
Counterfactual replay шага с подменой входа (golden context, fixed tool result, другая модель)
9
faults
Python lib + CLI
6 типов fault injection с ground-truth меткой в injections
10
api
FastAPI
Query blame/stats, replay jobs, webhook в GitHub Issues
**Data flow**
- 
App span → shim → OTLP → agent collector (no blocking: при отказе gateway спаны ждут в file queue, app не тормозит).
- 
Agent tier → loadbalancing по trace_id → gateway tier: все спаны трейса на одном инстансе, иначе tail sampling ломается.
- 
Gateway: normalizer → tail_sampling → ClickHouse spans + NATS spans.sampled.
- 
trace-assembler закрывает трейс → trace.completed.
- 
detectors читают трейс из ClickHouse, пишут findings (idempotent по (trace_id, detector, detector_version)).
- 
attributor пишет blame; если confidence < τ или трейс помечен для eval — ставит replay job.
- 
replayer перевыполняет шаг на изолированном инстансе и пишет counterfactuals; подтверждённая гипотеза повышает confidence.
- 
Grafana читает агрегаты; Alertmanager шлёт сдвиги blame share по bundle.digest и индексу.
**Attribution model.** Score компонента S_c = Σ w_d · s_d по findings, где w_d — веса из logistic regression на calibration split. Вердикт = argmax, если margin к второму месту > δ (старт 0.15), иначе unknown или multi. Replay подтверждает гипотезу: если подмена retrieval-контекста на golden чинит ответ (judge ≥ порога), retrieval подтверждён.
**Detectors**
Detector
Component
Сигнал
Стадия
retrieval_miss
retrieval
max score < τ, нет цитат на retrieved chunks, golden doc не в top-k (eval traffic)
MVP
context_truncation
orchestration
finish_reason=length или input_tokens ≥ 95% окна
MVP
tool_error_propagation
tool
ошибка tool span потреблена LLM-шагом без retry
MVP
schema_violation
model
structured output не парсится
MVP
loop
orchestration
(tool_name, args_hash) повторён ≥ 3 раз в agent span
MVP
prompt_regression
prompt
смена bundle.digest + падение judge score против baseline
Full
faithfulness_judge
model
LLM-as-judge, sampled, с budget cap
Full
provider_degradation
model
429/5xx storm, latency anomaly по provider
Full
cascade_early_warning
orchestration
признаки PrefixBench: рост контекста, повтор tool errors
Full
**API contracts**
```
GET  /v1/traces/{trace_id}/blame
GET  /v1/blame/stats?service=runbook-agent&window=1h&group_by=component,bundle_digest
POST /v1/replays            {"trace_id":"…","span_id":"…","substitution":{"kind":"golden_context","ref":"cuad:q-0412"}}
GET  /v1/replays/{replay_id}
POST /v1/injections         # internal, fault injector пишет ground truth
```

```
{
  "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736",
  "verdict": {"component": "retrieval", "confidence": 0.86, "status": "confirmed_by_replay"},
  "evidence": [
    {"detector": "retrieval_miss", "detector_version": "1.2.0", "span_id": "00f067aa0ba902b7", "weight": 0.61},
    {"detector": "faithfulness_judge", "detector_version": "0.4.1", "span_id": "a3ce929d0e0e4736", "weight": 0.22}
  ],
  "attributor_version": "0.3.0",
  "profile_version": "aegis-telemetry/1"
}
```

NATS event traceblame.finding.v1 и traceblame.blame.v1 — JSON Schema в aegis-platform/contracts.
**Storage schemas (ClickHouse)**
```
CREATE TABLE spans (
  trace_id FixedString(32), span_id FixedString(16), parent_span_id String,
  service LowCardinality(String), operation LowCardinality(String),
  span_kind LowCardinality(String),           -- llm|retrieval|tool|agent|chain
  start_ts DateTime64(6), duration_ms Float64, status_code LowCardinality(String),
  provider LowCardinality(String), model LowCardinality(String), finish_reason LowCardinality(String),
  input_tokens UInt32, output_tokens UInt32,
  tool_name LowCardinality(String), args_hash FixedString(16),
  retrieval_scores Array(Float32), retrieved_doc_ids Array(String),
  bundle_digest String, tenant_id LowCardinality(String), session_id String,
  content_ref String,                          -- s3://…, encrypted, TTL 7d, пусто без opt-in
  attrs Map(LowCardinality(String), String), profile_version LowCardinality(String)
) ENGINE = MergeTree
PARTITION BY toDate(start_ts)
ORDER BY (service, trace_id, start_ts)
TTL toDate(start_ts) + INTERVAL 30 DAY;

CREATE TABLE findings (
  finding_id UUID, trace_id FixedString(32), span_id FixedString(16),
  detector LowCardinality(String), detector_version LowCardinality(String),
  component LowCardinality(String), severity Enum8('info'=1,'warn'=2,'error'=3),
  score Float32, details String, created_at DateTime64(3)
) ENGINE = ReplacingMergeTree(created_at)
ORDER BY (trace_id, detector, detector_version, span_id);

CREATE TABLE blame (
  trace_id FixedString(32), component LowCardinality(String), confidence Float32,
  status LowCardinality(String), evidence String, attributor_version LowCardinality(String),
  bundle_digest String, service LowCardinality(String), created_at DateTime64(3)
) ENGINE = ReplacingMergeTree(created_at)
ORDER BY (service, trace_id, attributor_version);

CREATE TABLE injections (
  trace_id FixedString(32), fault_kind LowCardinality(String), target_component LowCardinality(String),
  params String, seed UInt32, injected_at DateTime64(3)
) ENGINE = MergeTree ORDER BY trace_id;
```

Replay jobs держатся в Postgres-таблице replay_jobs (state machine queued → running → done | failed) — в ClickHouse плохо делать update.
**Deployment topology**
Namespace
Workload
Replicas
Заметки
observability
otel-agent DaemonSet
1/node
file_storage queue, лимит 512 MiB
observability
otel-gateway Deployment
2–4
headless Service для loadbalancing resolver
observability
ClickHouse (Altinity operator)
1 shard × 1 replica в demo
в ADR — путь к 2 replicas + Keeper
observability
NATS JetStream
3
streams spans.sampled, trace.completed
traceblame
trace-assembler
2
leader-free, partition по hash(trace_id)
traceblame
detectors
1–6
KEDA NATS scaler
traceblame
attributor, api
2 / 2
PDB minAvailable 1
traceblame
replayer
0–2
Job-per-replay, NetworkPolicy без egress в prod-tools
observability
Grafana, Alertmanager, MinIO
1
dashboards и alerts как код

### 1.2 Repository structure
```
traceblame/
├── collector/                  # Go: custom distro
│   ├── builder-config.yaml     # ocb manifest, pinned component versions
│   ├── processor/normalizer/   # mapping tables per SDK + redaction
│   └── testdata/fixtures/      # golden spans: openinference, openllmetry, otel-native
├── services/
│   ├── assembler/              # Go
│   ├── api/                    # FastAPI
│   ├── detectors/              # plugins: detectors/<name>/{detector.py,tests/}

### 1.3 First 20 issues
#
Title
Milestone
1
ADR-0001: telemetry profile v1 on pinned OTel GenAI semconv
M0 Foundations
2
chore: bootstrap uv workspace, go.work, pre-commit, Makefile, kind cluster
M0
3
feat(schemas): aegis-telemetry v1 YAML + JSON Schema + contract test harness
M0
4
ci: lint/unit/contract pipeline with pinned scanners and cosign signing
M0
5
feat(collector): custom distro via ocb with normalizer stub
M1 Ingest
6
feat(normalizer): map OpenInference and OpenLLMetry attrs to profile v1
M1
7
feat(normalizer): redact and hash prompt/completion content
M1
8
feat(collector): two-tier topology with trace-id loadbalancing and tail_sampling
M1
9
feat(storage): ClickHouse migrations for spans, findings, blame, injections
M1
10
feat(sdk-py): instrument LangGraph Agentic RAG with aegis.* attrs
M1
11
feat(sdk-ts): instrument runbook agent
M1
12
feat(faults): six fault kinds with ground-truth labels and seeds
M2 Attribution
13
feat(detectors): plugin interface, versioning, idempotent findings
M2
14
feat(detectors): retrieval_miss, context_truncation, tool_error_propagation, schema_violation, loop
M2
15
feat(attributor): weighted scoring with unknown margin and calibrated weights
M2
16
feat(api): blame and stats endpoints + OpenAPI + schemathesis
M2
17
docs(evals): confusion matrix report on 600 injected traces
M2
18
feat(obs): dashboards-as-code, Sloth SLOs, alert rules with runbook links
M3 Ops
19
perf(bench): instrumentation overhead and collector throughput baselines
M3
20
feat(deploy): Helm chart, Argo CD app, make demo with recorded walkthrough
M3

### 1.4 CI/CD pipeline
Stage
Что запускается
Quality gate
static
ruff, pyright, golangci-lint, hadolint, helm lint, kubeconform
0 errors
unit
pytest, go test -race
coverage ≥ 80% для normalizer, detectors, attributor
contract
golden fixtures → normalizer → JSON Schema profile v1; schemathesis по OpenAPI
100% required attrs, 0 schema breaks
integration
testcontainers: ClickHouse + NATS; span → finding → blame
end-to-end < 10 s
eval-gate
frozen D1-test, fixed seeds
top-1 ≥ 0.80, macro-F1 ≥ 0.75, падение ≤ 2 pp против main
perf-smoke
collector throughput на runner
регрессия ≤ 10% против baseline
security
gitleaks, Grype/Trivy pinned by digest, Syft SBOM
0 critical без waiver
release
release-please → images + Helm chart в OCI, cosign, SLSA provenance
подпись проверяется в admission (Kyverno)
deploy
Argo CD: staging → prod demo-кластер
smoke + SLO check 30 min
**Deployment strategy.** Analysis-сервисы (detectors, attributor) выкатываются в **shadow**: новая версия пишет blame со своим attributor_version, dashboard показывает agreement с текущей и accuracy на injection-трафике; promote после 24 h. Collector выкатывается через второй gateway pool на часть сервисов.
**Rollback plan.**
- 
Сервисы: Argo CD revert на предыдущий chart digest, < 5 min.
- 
Collector config: предыдущий ConfigMap revision; agents держат спаны в file queue во время переключения, потеря = 0.
- 
ClickHouse: только forward migrations по схеме expand → migrate → contract; rollback кода совместим с N−1 схемой.
- 
Profile: normalizer поддерживает v(N) и v(N−1) одновременно.
- 
Trigger: span drop rate > 0.1% за 10 min или burn rate freshness SLO > 2× → автоматический revert.

### 1.5 Observability plan
TraceBlame наблюдает за собой через отдельный self-telemetry pipeline без детекторов — иначе рекурсия.
**Metrics**
- 
Collector: otelcol_receiver_accepted_spans, otelcol_exporter_send_failed_spans, otelcol_exporter_queue_size, tail sampling decisions.
- 
traceblame_normalizer_unmapped_attrs_total{sdk,attr} — ранний сигнал semconv drift.
- 
traceblame_normalizer_newer_schema_spans_total{sdk,schema_url} — спаны со schema_url новее известных mapping tables (map + pass-through, ADR-0001).
- 
traceblame_pipeline_lag_seconds (span end → blame written), histogram.
- 
traceblame_findings_total{detector,component}, traceblame_detector_duration_seconds.
- 
traceblame_blame_total{service,component,bundle_digest}, traceblame_unknown_ratio.
- 
traceblame_judge_cost_usd_total{detector}, traceblame_replay_jobs{state}.
**Logs.** JSON, обязательные поля trace_id, detector, version; никакого content. Loki или ClickHouse logs table.
**Traces.** Pipeline-шаги (assemble, detect, attribute) — свои спаны с link на анализируемый трейс.
**Dashboards**
- 
Pipeline health: RED по каждому тиру, queue size, lag.
- 
Blame overview: stacked share по component во времени, разрез по bundle_digest и index alias.
- 
Detector quality: FPR на clean canary-трафике, agreement между версиями.
- 
Judge cost: расход против daily budget.
**SLOs:** span acceptance 99.5% / 28 d; freshness p95 < 60 s; MTTD деградации < 15 min.
**Alerts**
Alert
Условие
Severity
SpanDropRateHigh
dropped / accepted > 0.1% за 10 min
page
PipelineLagHigh
lag p95 > 60 s за 15 min
ticket
UnmappedAttrsSpike
рост unmapped attrs по SDK > 10× от 7d baseline
ticket
BlameShareShift
share компонента +15 pp против 7d baseline после смены bundle или индекса
notify owner
UnknownVerdictHigh
unknown > 30% за 1 h
ticket
JudgeBudgetBurn
> 80% daily budget
ticket, judge sampling ↓
Каждый alert ссылается на docs/runbooks/<alert>.md.

### 1.6 Evaluation plan
**Datasets**
ID
Состав
Размер
Источник
D1
Single-fault трейсы: 6 fault kinds × 75 + 150 clean
600
реальные вопросы CUAD, задачи runbook agent, Agentic RAG; faults через faults/
D2
Multi-fault (2 дефекта)
100
то же
D3
Clean canary-трафик
непрерывно
demo-кластер
D4
Golden span fixtures по SDK и версиям
~60
OpenInference, OpenLLMetry, OTel native
D5
Каскадные отказы
100
сценарии PrefixBench
Сплит calibration/test по исходному запросу, не по трейсу — иначе leakage. Seeds и версии моделей фиксируются в evals/manifest.yaml, датасеты под DVC или hash manifest.
**Offline metrics:** per-class precision/recall, macro-F1, top-1 accuracy, ECE confidence, unknown rate, replay confirmation rate, detector FPR на clean.
**Online metrics:** MTTD против таймлайна внедрённого инцидента, стабильность blame share, доля alerts, размеченных как полезные, overhead.
**Regression suite:** D1-test + D2 + D4 заморожены и прогоняются в CI; каждый реальный мисклассифицированный инцидент добавляется в evals/regressions/.
**Acceptance thresholds**
Metric
Threshold
Top-1 accuracy, single-fault (D1-test)
≥ 0.80
Macro-F1 (D1-test)
≥ 0.75
Top-2 contains both faults (D2)
≥ 0.60
Detector FPR на clean
≤ 5%
Unknown rate, single-fault
≤ 15%
ECE
≤ 0.10
App overhead p95
≤ 3%
Pipeline lag p95
≤ 60 s
Normalizer coverage required attrs (D4)
100%

### 1.7 Interview questions
#
Вопрос
Чем закрываете
1
Зачем своя атрибуция, если есть Langfuse/Phoenix?
Они показывают трейс; TraceBlame выдаёт вердикт с confusion matrix на ground truth
2
Как жить с нестабильным GenAI semconv?
Pinned version, normalizer с mapping tables, contract fixtures, alert на unmapped attrs
3
Tail sampling при нескольких репликах collector?
Двухуровневая топология, loadbalancing по trace_id, замер потерь при скейлинге
4
Retrieval вернул документы — как понять, что виноват всё равно он?
Golden-doc recall, citation coverage, counterfactual replay с golden context
5
Откуда ground truth для валидации?
Fault injector с метками и seeds, сплит по запросу
6
Что при двух дефектах сразу?
multi/unknown по margin, top-2 метрика на D2, калибровка ECE
7
PII в prompt/completion?
Content off по умолчанию, hashing в processor, encrypted blobs с TTL 7 d, доступ по роли
8
Сколько стоит LLM-as-judge и как его ограничить?
Sampling, daily budget, judge только на low-confidence трейсах
9
Что если collector лёг?
File queue, fail-open для app, замеренная потеря = 0 в drill
10
Как связать рост ошибок с конкретным изменением?
bundle.digest и index alias в каждом спане, BlameShareShift alert