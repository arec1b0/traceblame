package normalizer

import (
	"context"

	"go.opentelemetry.io/collector/component"
	"go.opentelemetry.io/collector/consumer"
	"go.opentelemetry.io/collector/processor"
	"go.opentelemetry.io/collector/processor/processorhelper"
)

const stability = component.StabilityLevelDevelopment

var componentType = component.MustNewType("normalizer")

// NewFactory returns the factory of the normalizer processor.
func NewFactory() processor.Factory {
	return processor.NewFactory(componentType, createDefaultConfig,
		processor.WithTraces(createTraces, stability))
}

func createDefaultConfig() component.Config {
	return &Config{}
}

func createTraces(
	ctx context.Context, set processor.Settings, cfg component.Config, next consumer.Traces,
) (processor.Traces, error) {
	p := newProcessor()
	return processorhelper.NewTraces(ctx, set, cfg, next, p.processTraces,
		processorhelper.WithCapabilities(consumer.Capabilities{MutatesData: false}))
}
