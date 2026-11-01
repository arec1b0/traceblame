/**
 * The aegis.* attributes the shim sets, with the scope and pattern the profile gives them
 * (src/contract.json; tests/contract/test_shim_contract.py checks it against the profile).
 */
import contract from "./contract.json" with { type: "json" };

export const TENANT_ID = "aegis.tenant.id";
export const EVAL_RUN_ID = "aegis.eval.run.id";
export const SESSION_ID = "aegis.session.id";
export const BUNDLE_DIGEST = "aegis.bundle.digest";
export const DECISION_ID = "aegis.decision.id";
export const FAULT_INJECTION_ID = "aegis.fault.injection.id";
export const ARGS_HASH = "aegis.tool.args_hash";
export const PARSE_OK = "aegis.output.parse_ok";
export const HTTP_STATUS = "http.response.status_code";
export const OPERATION_ID = "ai.operationId";

export const SCOPES: Readonly<Record<string, string>> = contract.scopes;
const PATTERNS: Readonly<Record<string, string>> = contract.patterns;

/** value, if it is a non-empty string that fits the profile pattern of name; else throws. */
export function checked(name: string, value: string): string {
  if (typeof value !== "string" || value === "") {
    throw new TypeError(`${name} must be a non-empty string`);
  }
  const pattern = PATTERNS[name];
  // Without the m flag, `$` matches only at the very end, so a trailing newline fails.
  if (pattern !== undefined && !new RegExp(pattern).test(value)) {
    throw new RangeError(`${name} does not match the profile pattern ${pattern}`);
  }
  return value;
}
