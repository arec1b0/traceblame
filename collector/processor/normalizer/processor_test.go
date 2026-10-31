package normalizer

import (
	"context"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.opentelemetry.io/collector/component/componenttest"
	"go.opentelemetry.io/collector/consumer/consumertest"
	"go.opentelemetry.io/collector/pdata/ptrace"
	"go.opentelemetry.io/collector/processor/processortest"

	"github.com/arec1b0/traceblame/collector/internal/fixtures"
)

func marshalJSON(t *testing.T, td ptrace.Traces) string {
	t.Helper()
	b, err := (&ptrace.JSONMarshaler{}).MarshalTraces(td)
	require.NoError(t, err)
	return string(b)
}

func TestNoOpPassesEveryFixtureThroughUnchanged(t *testing.T) {
	for _, fx := range fixtures.All(t) {
		t.Run(fx.ID, func(t *testing.T) {
			td := fx.Load(t)
			want := marshalJSON(t, td)

			sink := new(consumertest.TracesSink)
			f := NewFactory()
			p, err := f.CreateTraces(context.Background(), processortest.NewNopSettings(f.Type()),
				f.CreateDefaultConfig(), sink)
			require.NoError(t, err)
			require.NoError(t, p.Start(context.Background(), componenttest.NewNopHost()))
			t.Cleanup(func() { require.NoError(t, p.Shutdown(context.Background())) })

			require.NoError(t, p.ConsumeTraces(context.Background(), td))
			got := sink.AllTraces()
			require.Len(t, got, 1)
			assert.JSONEq(t, want, marshalJSON(t, got[0]))
		})
	}
}
