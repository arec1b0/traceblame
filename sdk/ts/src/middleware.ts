/**
 * http.response.status_code for AI SDK calls (ADR-0006 section 6). The AI SDK's error text does
 * not carry the status, so the normalizer cannot read it (#29); the error object does.
 *
 * Add it to the model: `wrapLanguageModel({ model, middleware: statusMiddleware })`. It runs
 * inside each attempt's doGenerate span, so each attempt records its own status.
 */
import { trace } from "@opentelemetry/api";
import { APICallError, type LanguageModelV1Middleware, RetryError } from "ai";
import { HTTP_STATUS, OPERATION_ID } from "./attributes.js";

const ATTEMPT = /\.(doGenerate|doStream)$/;

function recordStatus(error: unknown): void {
  const span = trace.getActiveSpan();
  // Only on the attempt span: with telemetry off, the active span is the app's own.
  const attributes = (span as { attributes?: Record<string, unknown> } | undefined)?.attributes;
  const operation = attributes?.[OPERATION_ID];
  if (span === undefined || typeof operation !== "string" || !ATTEMPT.test(operation)) return;
  const cause = RetryError.isInstance(error) ? error.lastError : error;
  if (!APICallError.isInstance(cause)) return;
  const status = cause.statusCode;
  if (typeof status === "number" && Number.isInteger(status) && status >= 400 && status <= 599) {
    span.setAttribute(HTTP_STATUS, status);
  }
}

export const statusMiddleware: LanguageModelV1Middleware = {
  wrapGenerate: async ({ doGenerate }) => {
    try {
      return await doGenerate();
    } catch (error) {
      recordStatus(error);
      throw error;
    }
  },
  wrapStream: async ({ doStream }) => {
    try {
      return await doStream();
    } catch (error) {
      recordStatus(error);
      throw error;
    }
  },
};
