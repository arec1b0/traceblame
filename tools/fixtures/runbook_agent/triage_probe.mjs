// Re-runs the app's own triage on recorded alerts (#13, the report's "Model behaviour").
// Runs inside the agent pod (`node --input-type=module -e <this> <alerts JSON> <repeats>`):
// the app's build, configuration, model and network path, without its tracing (nothing is
// exported) and without executing anything. History is empty, as it was for each task's own
// triage (every task has its own service). Prints one JSON line per alert: the alert name, the
// candidate runbook ids and, per repeat, apply/runbookId as the model chose them and as the app
// returned them; never the reasoning or any text.
import { loadConfig } from "/app/dist/config/config.js";
import { RunbookRegistry } from "/app/dist/runbooks/runbook.registry.js";
import { DEFAULT_RUNBOOKS } from "/app/dist/runbooks/default.runbooks.js";
import { ExecFileRunner } from "/app/dist/runbooks/actions/command.runner.js";
import { RestartServiceAction } from "/app/dist/runbooks/actions/restart-service.action.js";
import { ScaleDeploymentAction } from "/app/dist/runbooks/actions/scale-deployment.action.js";
import { CollectDiagnosticsAction } from "/app/dist/runbooks/actions/collect-diagnostics.action.js";
import { createLanguageModel } from "/app/dist/agent/llm.provider.js";
import { TriageAgent } from "/app/dist/agent/agent.js";
import { createTrace } from "/app/dist/observability/trace.js";

const [alertsJson, repeatsArg] = process.argv.slice(1);
const alerts = JSON.parse(alertsJson);
const repeats = Number(repeatsArg);
const config = loadConfig();
// The agent logs the model's decision (info: apply, runbookId) and a runbook id outside the
// candidates (warn: runbookId) before it turns that into a decline. Keep only those two fields;
// an error is only marked, since its object can carry the response body.
let seen = {};
const logger = {
  info: (fields) => { if (fields && "apply" in fields) seen = { apply: fields.apply, runbookId: fields.runbookId }; },
  warn: (fields) => { if (fields && "runbookId" in fields) seen = { apply: true, runbookId: fields.runbookId, outsideCandidates: true }; },
  error: () => { seen = { error: true }; },
  debug: () => {},
  trace: () => {},
  child: () => logger,
};
const runner = new ExecFileRunner();
const registry = new RunbookRegistry();
registry.registerAction(new CollectDiagnosticsAction(runner));
registry.registerAction(new RestartServiceAction(runner));
registry.registerAction(new ScaleDeploymentAction(runner));
for (const runbook of DEFAULT_RUNBOOKS) registry.registerRunbook(runbook);
const agent = new TriageAgent(createLanguageModel(config));

for (const alert of alerts) {
  const candidates = registry.findMatching(alert).map((runbook) => runbook.id);
  const decisions = [];
  for (let i = 0; i < repeats; i++) {
    seen = {};
    const decision = await agent.triage(alert, registry.findMatching(alert), [], logger, createTrace());
    // What the model chose (seen), and what the app made of it (decision).
    decisions.push({ model: seen, app: { apply: decision.apply, runbookId: decision.runbookId } });
  }
  process.stdout.write(JSON.stringify({ alert: alert.name, candidates, decisions }) + "\n");
}
