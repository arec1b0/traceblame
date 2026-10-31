package normalizer

import (
	"context"
	"strings"
	"sync"

	"go.opentelemetry.io/collector/component"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/metric"
	"go.uber.org/zap"
)

const (
	meterName = "github.com/arec1b0/traceblame/collector/processor/normalizer"
	// overflowLabel replaces attr / schema_url once a counter reached MaxLabelValues.
	overflowLabel = "_other"
)

// telemetry holds the normalizer's own counters under their design §1.5 names. The
// instruments are made by hand, not with mdatagen, which would prefix them with otelcol_.
// The names carry _total because the collector's Prometheus endpoint adds no type suffix
// (it exposes otelcol_receiver_accepted_spans, a counter, as is); a translator that does
// add suffixes leaves a name that already ends in _total alone.
type telemetry struct {
	logger *zap.Logger
	limit  int

	unmapped       *capped
	newerSchema    *capped
	invalid        *capped
	droppedLarge   *capped
	markerRejected *capped
}

func newTelemetry(set component.TelemetrySettings, limit int) (*telemetry, error) {
	meter := set.MeterProvider.Meter(meterName)
	t := &telemetry{logger: set.Logger, limit: limit}
	var err error
	if t.unmapped, err = t.counter(meter, "traceblame_normalizer_unmapped_attrs_total", "{attribute}",
		"Attributes passed through that neither the profile nor the span's mapping table knows.",
		"attr", "unmapped attribute"); err != nil {
		return nil, err
	}
	if t.newerSchema, err = t.counter(meter, "traceblame_normalizer_newer_schema_spans_total", "{span}",
		"Spans whose schema_url is newer than every mapping table of their SDK.",
		"schema_url", "schema_url newer than every mapping table"); err != nil {
		return nil, err
	}
	if t.invalid, err = t.counter(meter, "traceblame_normalizer_invalid_attrs_total", "{attribute}",
		"Profile attributes the normalizer could not repair: mistyped or constraint-failing "+
			"values (dropped), unknown aegis.* keys (dropped), operations outside the profile (kept).",
		"attr", "invalid profile attribute"); err != nil {
		return nil, err
	}
	if t.droppedLarge, err = t.counter(meter, "traceblame_normalizer_dropped_large_values_total", "{attribute}",
		"Unmapped attributes dropped from GenAI spans because their value exceeds "+
			"max_unmapped_value_bytes (fail closed on unknown content, ADR-0002).",
		"attr", "large unmapped value dropped"); err != nil {
		return nil, err
	}
	// A security signal (#36): a marker would force 100% sampling. Labelled by tenant, not
	// by sdk; the run or injection id itself is never a label or a log field.
	if t.markerRejected, err = t.counterWith(meter, "traceblame_normalizer_eval_marker_rejected_total", "{attribute}",
		"Eval and fault-injection markers dropped: the tenant is not in eval_tenants "+
			"(tenant_not_allowed), the marker is in the wrong scope (scope), or the value "+
			"fails the profile (pattern).",
		[]string{"tenant", "attr", "reason"}, 0, "eval marker rejected"); err != nil {
		return nil, err
	}
	return t, nil
}

// counter makes a counter labelled {sdk, <label>} whose <label> is capped.
func (t *telemetry) counter(meter metric.Meter, name, unit, desc, label, logMsg string) (*capped, error) {
	return t.counterWith(meter, name, unit, desc, []string{"sdk", label}, 1, logMsg)
}

func (t *telemetry) counterWith(meter metric.Meter, name, unit, desc string, labels []string, capAt int, logMsg string) (*capped, error) {
	c, err := meter.Int64Counter(name, metric.WithUnit(unit), metric.WithDescription(desc))
	if err != nil {
		return nil, err
	}
	return &capped{counter: c, labels: labels, capAt: capAt, limit: t.limit, logger: t.logger, logMsg: logMsg, seen: map[string]bool{}}, nil
}

// capped is a counter that records at most limit label sets; further sets are counted with
// labels[capAt] replaced by overflowLabel. The first sighting of each set is logged once:
// keys and ids only, never attribute values.
type capped struct {
	counter metric.Int64Counter
	labels  []string
	capAt   int
	limit   int
	logger  *zap.Logger
	logMsg  string

	mu       sync.Mutex
	seen     map[string]bool
	overflow bool
}

// add counts one {sdk, <label>} sighting.
func (c *capped) add(ctx context.Context, sdk, value, traceID string) {
	c.addLabels(ctx, traceID, sdk, value)
}

// addLabels counts one sighting with a value for each label, in order.
func (c *capped) addLabels(ctx context.Context, traceID string, values ...string) {
	values = append([]string(nil), values...)
	key := strings.Join(values, "\x00")
	c.mu.Lock()
	switch {
	case c.seen[key]:
	case len(c.seen) < c.limit:
		c.seen[key] = true
		fields := []zap.Field{zap.String("trace_id", traceID)}
		for i, l := range c.labels {
			fields = append(fields, zap.String(l, values[i]))
		}
		c.logger.Info(c.logMsg, append(fields, zap.String("normalizer_version", Version))...)
	default:
		values[c.capAt] = overflowLabel
		if !c.overflow {
			c.overflow = true
			c.logger.Warn("label cap reached; further values are counted as "+overflowLabel,
				zap.String("trace_id", traceID), zap.String("metric_label", c.labels[c.capAt]),
				zap.Int("max_label_values", c.limit), zap.String("normalizer_version", Version))
		}
	}
	c.mu.Unlock()
	attrs := make([]attribute.KeyValue, len(c.labels))
	for i, l := range c.labels {
		attrs[i] = attribute.String(l, values[i])
	}
	c.counter.Add(ctx, 1, metric.WithAttributes(attrs...))
}

// attrLabel collapses array indexes, so llm.tools.0.tool.name and llm.tools.1.tool.name
// share the label llm.tools.*.tool.name.
func attrLabel(name string) string {
	parts := strings.Split(name, ".")
	for i, p := range parts {
		if p != "" && strings.Trim(p, "0123456789") == "" {
			parts[i] = "*"
		}
	}
	return strings.Join(parts, ".")
}
