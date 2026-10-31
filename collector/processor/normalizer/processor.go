package normalizer

import (
	"context"

	"go.opentelemetry.io/collector/pdata/ptrace"
)

// normalizerProcessor is the stub registered in the distribution: it forwards spans
// unchanged.
type normalizerProcessor struct{}

func newProcessor() *normalizerProcessor {
	return &normalizerProcessor{}
}

func (*normalizerProcessor) processTraces(_ context.Context, td ptrace.Traces) (ptrace.Traces, error) {
	return td, nil
}
