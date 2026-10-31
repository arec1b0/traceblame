package normalizer

import (
	"context"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.opentelemetry.io/collector/component"
	"go.opentelemetry.io/collector/component/componenttest"
	"go.opentelemetry.io/collector/confmap"
	"go.opentelemetry.io/collector/consumer/consumertest"
	"go.opentelemetry.io/collector/processor/processortest"
)

func TestFactoryRegistersTheNormalizerType(t *testing.T) {
	f := NewFactory()
	assert.Equal(t, "normalizer", f.Type().String())
	assert.Equal(t, component.StabilityLevelDevelopment, f.TracesStability())
}

func TestDefaultConfigIsValid(t *testing.T) {
	cfg := NewFactory().CreateDefaultConfig()
	require.NoError(t, componenttest.CheckConfigStruct(cfg))
	require.NoError(t, confmap.Validate(cfg))
}

func TestLifecycle(t *testing.T) {
	f := NewFactory()
	p, err := f.CreateTraces(context.Background(), processortest.NewNopSettings(f.Type()),
		f.CreateDefaultConfig(), consumertest.NewNop())
	require.NoError(t, err)
	require.NoError(t, p.Start(context.Background(), componenttest.NewNopHost()))
	require.NoError(t, p.Shutdown(context.Background()))
}
