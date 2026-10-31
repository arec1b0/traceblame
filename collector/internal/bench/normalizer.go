package bench

import (
	"context"

	"go.opentelemetry.io/collector/component/componenttest"
	"go.opentelemetry.io/collector/confmap"
	"go.opentelemetry.io/collector/consumer/consumertest"
	"go.opentelemetry.io/collector/pdata/ptrace"
	"go.opentelemetry.io/collector/processor/processortest"

	"github.com/arec1b0/traceblame/collector/processor/normalizer"
)

// Normalizer runs the normalizer of a collector config (agent.yaml or gateway.yaml) on
// traces, as that tier does, so a replay sees what the gateway's tail sampling sees.
type Normalizer struct {
	conf map[string]any
}

// NewNormalizer reads processors.normalizer of a collector config, with ${env:X} taken from
// env.
func NewNormalizer(config string, env map[string]string) (*Normalizer, error) {
	conf, err := processorConfig(config, "normalizer", env)
	if err != nil {
		return nil, err
	}
	return &Normalizer{conf: conf}, nil
}

// Run normalizes td with a fresh processor; ctx carries the client info (the authenticated
// tenant on the agent tier).
func (n *Normalizer) Run(ctx context.Context, td ptrace.Traces) (ptrace.Traces, error) {
	f := normalizer.NewFactory()
	cfg := f.CreateDefaultConfig()
	if err := confmap.NewFromStringMap(n.conf).Unmarshal(cfg); err != nil {
		return td, err
	}
	if v, ok := cfg.(interface{ Validate() error }); ok {
		if err := v.Validate(); err != nil {
			return td, err
		}
	}
	sink := new(consumertest.TracesSink)
	p, err := f.CreateTraces(context.Background(), processortest.NewNopSettings(f.Type()), cfg, sink)
	if err != nil {
		return td, err
	}
	if err := p.Start(context.Background(), componenttest.NewNopHost()); err != nil {
		return td, err
	}
	err = p.ConsumeTraces(ctx, td)
	if shutdownErr := p.Shutdown(context.Background()); err == nil {
		err = shutdownErr
	}
	if err != nil {
		return td, err
	}
	out := ptrace.NewTraces()
	for _, got := range sink.AllTraces() {
		got.ResourceSpans().MoveAndAppendTo(out.ResourceSpans())
	}
	return out, nil
}
