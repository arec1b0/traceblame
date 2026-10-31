package bench

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"time"

	"github.com/open-telemetry/opentelemetry-collector-contrib/processor/tailsamplingprocessor"
	"go.opentelemetry.io/collector/component/componenttest"
	"go.opentelemetry.io/collector/confmap"
	"go.opentelemetry.io/collector/consumer/consumertest"
	"go.opentelemetry.io/collector/pdata/pcommon"
	"go.opentelemetry.io/collector/pdata/ptrace"
	"go.opentelemetry.io/collector/processor/processortest"
	"go.yaml.in/yaml/v3"
)

// replayWait is the decision_wait of a replay: decisions come from span data (status,
// timestamps, trace id), not from wall-clock time, so a short wait gives the same result.
const replayWait = time.Second

var envRef = regexp.MustCompile(`\$\{env:([A-Za-z0-9_]+)\}`)

// TailSampler replays the gateway's tail_sampling policies on whole traces or on parts of
// one, with the real processor (contrib v0.164.1): it tells a drop decision from a loss.
type TailSampler struct {
	conf map[string]any
}

// NewTailSampler reads processors.tail_sampling of a gateway config, with ${env:X} taken
// from env.
func NewTailSampler(gatewayConfig string, env map[string]string) (*TailSampler, error) {
	conf, err := processorConfig(gatewayConfig, "tail_sampling", env)
	if err != nil {
		return nil, err
	}
	conf["decision_wait"] = replayWait.String()
	conf["num_traces"] = 2_000_000
	return &TailSampler{conf: conf}, nil
}

// processorConfig reads processors.<name> of a collector config, with every ${env:X} in it
// taken from env; references elsewhere in the file (credentials) are not needed.
func processorConfig(config, name string, env map[string]string) (map[string]any, error) {
	raw, err := os.ReadFile(filepath.Clean(config))
	if err != nil {
		return nil, err
	}
	var doc struct {
		Processors map[string]yaml.Node `yaml:"processors"`
	}
	if err := yaml.Unmarshal(raw, &doc); err != nil {
		return nil, err
	}
	node, ok := doc.Processors[name]
	if !ok {
		return nil, fmt.Errorf("bench: %s has no %s processor", config, name)
	}
	section, err := yaml.Marshal(&node)
	if err != nil {
		return nil, err
	}
	var missing error
	expanded := envRef.ReplaceAllStringFunc(string(section), func(ref string) string {
		v, ok := env[envRef.FindStringSubmatch(ref)[1]]
		if !ok && missing == nil {
			missing = fmt.Errorf("bench: no value for %s in %s", ref, config)
		}
		return v
	})
	if missing != nil {
		return nil, missing
	}
	conf := map[string]any{}
	if err := yaml.Unmarshal([]byte(expanded), &conf); err != nil {
		return nil, err
	}
	return conf, nil
}

// Decide feeds every part (one trace id each) to a fresh processor and returns the trace
// ids it samples.
func (s *TailSampler) Decide(parts []ptrace.Traces) (map[[16]byte]bool, error) {
	f := tailsamplingprocessor.NewFactory()
	cfg := f.CreateDefaultConfig()
	if err := confmap.NewFromStringMap(s.conf).Unmarshal(cfg); err != nil {
		return nil, err
	}
	sink := new(consumertest.TracesSink)
	ctx := context.Background()
	p, err := f.CreateTraces(ctx, processortest.NewNopSettings(f.Type()), cfg, sink)
	if err != nil {
		return nil, err
	}
	if err := p.Start(ctx, componenttest.NewNopHost()); err != nil {
		return nil, err
	}
	want := map[[16]byte]bool{}
	for _, td := range parts {
		for i := 0; i < td.ResourceSpans().Len(); i++ {
			want[[16]byte(firstTraceID(td))] = true
		}
		if err := p.ConsumeTraces(ctx, td); err != nil {
			return nil, err
		}
	}
	// Decisions are made on a 1 s tick once a trace is older than decision_wait.
	time.Sleep(2*replayWait + 1500*time.Millisecond)
	if err := p.Shutdown(ctx); err != nil {
		return nil, err
	}
	out := map[[16]byte]bool{}
	for _, td := range sink.AllTraces() {
		for i := 0; i < td.ResourceSpans().Len(); i++ {
			rs := td.ResourceSpans().At(i)
			for j := 0; j < rs.ScopeSpans().Len(); j++ {
				spans := rs.ScopeSpans().At(j).Spans()
				for k := 0; k < spans.Len(); k++ {
					out[[16]byte(spans.At(k).TraceID())] = true
				}
			}
		}
	}
	return out, nil
}

func firstTraceID(td ptrace.Traces) pcommon.TraceID {
	return td.ResourceSpans().At(0).ScopeSpans().At(0).Spans().At(0).TraceID()
}
