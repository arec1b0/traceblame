// Package assembler is the trace-assembler: it marks a trace complete (root span end plus
// a grace period), writes trace_summary and publishes trace.completed (built in M1).
package assembler
