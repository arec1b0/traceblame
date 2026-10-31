// Command traceblame-normalize runs the normalizer processor over one OTLP/JSON trace
// export: stdin to stdout. The contract tests call it through
// traceblame_contracts.normalizers.GoNormalizer, so golden fixtures are checked against the
// profile schema after the same code the collector runs.
//
// Usage: traceblame-normalize [--config normalizer.yaml] < export.otlp.json
package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"

	"go.opentelemetry.io/collector/component/componenttest"
	"go.opentelemetry.io/collector/confmap"
	"go.opentelemetry.io/collector/consumer/consumertest"
	"go.opentelemetry.io/collector/pdata/ptrace"
	"go.opentelemetry.io/collector/processor/processortest"
	"go.yaml.in/yaml/v3"

	"github.com/arec1b0/traceblame/collector/processor/normalizer"
)

func main() {
	if err := run(os.Args[1:], os.Stdin, os.Stdout); err != nil {
		fmt.Fprintln(os.Stderr, "traceblame-normalize:", err)
		os.Exit(1)
	}
}

func run(args []string, in io.Reader, out io.Writer) error {
	flags := flag.NewFlagSet("traceblame-normalize", flag.ContinueOnError)
	configPath := flags.String("config", "", "normalizer settings, YAML (default: the processor defaults)")
	if err := flags.Parse(args); err != nil {
		return err
	}

	f := normalizer.NewFactory()
	cfg := f.CreateDefaultConfig()
	if *configPath != "" {
		raw, err := os.ReadFile(*configPath)
		if err != nil {
			return err
		}
		var settings map[string]any
		if err := yaml.Unmarshal(raw, &settings); err != nil {
			return fmt.Errorf("%s: %w", *configPath, err)
		}
		if err := confmap.NewFromStringMap(settings).Unmarshal(cfg); err != nil {
			return fmt.Errorf("%s: %w", *configPath, err)
		}
	}
	if err := confmap.Validate(cfg); err != nil {
		return err
	}

	raw, err := io.ReadAll(in)
	if err != nil {
		return err
	}
	td, err := (&ptrace.JSONUnmarshaler{}).UnmarshalTraces(raw)
	if err != nil {
		return fmt.Errorf("stdin is not OTLP/JSON: %w", err)
	}

	ctx := context.Background()
	sink := new(consumertest.TracesSink)
	p, err := f.CreateTraces(ctx, processortest.NewNopSettings(f.Type()), cfg, sink)
	if err != nil {
		return err
	}
	if err := p.Start(ctx, componenttest.NewNopHost()); err != nil {
		return err
	}
	err = p.ConsumeTraces(ctx, td)
	err = errors.Join(err, p.Shutdown(ctx))
	if err != nil {
		return err
	}
	result := ptrace.NewTraces()
	for _, t := range sink.AllTraces() {
		t.ResourceSpans().MoveAndAppendTo(result.ResourceSpans())
	}
	b, err := (&ptrace.JSONMarshaler{}).MarshalTraces(result)
	if err != nil {
		return err
	}
	_, err = out.Write(b)
	return err
}
