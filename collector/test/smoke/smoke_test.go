package smoke

import (
	"encoding/json"
	"os"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	"github.com/arec1b0/traceblame/collector/internal/fixtures"
)

func TestFixturesPassThroughTheDistribution(t *testing.T) {
	c := startCollector(t, "", "info")
	all := fixtures.All(t)
	want := map[string]string{}
	for _, fx := range all {
		raw, err := os.ReadFile(fx.Path)
		require.NoError(t, err)
		c.post(t, raw)
		for k, v := range spanDocs(t, fx.Load(t)) {
			want[k] = v
		}
	}
	assert.Contains(t, c.metrics(t), "otelcol_receiver_accepted_spans")

	got := spanDocs(t, c.exported(t)...)
	require.Len(t, got, len(want))
	for k, w := range want {
		assert.JSONEq(t, w, got[k], k)
	}
}

func TestLogsAreJSON(t *testing.T) {
	c := startCollector(t, "", "info")
	lines := c.logLines(t)
	require.NotEmpty(t, lines)
	for _, line := range lines {
		var entry map[string]any
		assert.NoError(t, json.Unmarshal([]byte(line), &entry), line)
	}
}
