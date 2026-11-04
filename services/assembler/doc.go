// Package assembler is the trace-assembler (ADR-0003): it polls the ClickHouse arrival
// watermark, writes trace_summary and publishes trace.completed. The binary is cmd/assembler;
// the code is under internal/.
package assembler
