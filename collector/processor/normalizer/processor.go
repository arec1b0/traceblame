package normalizer

import (
	"context"
	"strings"

	"go.opentelemetry.io/collector/component"
	"go.opentelemetry.io/collector/pdata/pcommon"
	"go.opentelemetry.io/collector/pdata/ptrace"

	"github.com/arec1b0/traceblame/collector/processor/normalizer/internal/mapping"
	"github.com/arec1b0/traceblame/collector/processor/normalizer/internal/profile"
)

const (
	operationAttr = "gen_ai.operation.name"
	tenantAttr    = "aegis.tenant.id"
	aegisPrefix   = "aegis."
	fallbackKind  = "chain"
)

// normalizerProcessor maps SDK spans to the profile (ADR-0001, adapter layer): detect the
// SDK, map with its table, check every profile attribute, stamp the span. It never drops a
// span and never guesses a rename: attributes nobody knows pass through and are counted.
type normalizerProcessor struct {
	cfg     *Config
	profile *profile.Profile
	tables  *mapping.Registry
	emit    emitter
	tel     *telemetry
}

func newProcessor(cfg *Config, set component.TelemetrySettings) (*normalizerProcessor, error) {
	p, err := loadProfile(cfg.Profile)
	if err != nil {
		return nil, err
	}
	tables, err := loadTables(p)
	if err != nil {
		return nil, err
	}
	tel, err := newTelemetry(set, cfg.MaxLabelValues)
	if err != nil {
		return nil, err
	}
	return &normalizerProcessor{cfg: cfg, profile: p, tables: tables, emit: emitters[cfg.Profile], tel: tel}, nil
}

func (p *normalizerProcessor) processTraces(ctx context.Context, td ptrace.Traces) (ptrace.Traces, error) {
	for i := 0; i < td.ResourceSpans().Len(); i++ {
		rs := td.ResourceSpans().At(i)
		p.normalizeResource(ctx, rs.Resource().Attributes())
		for j := 0; j < rs.ScopeSpans().Len(); j++ {
			ss := rs.ScopeSpans().At(j)
			scope := mapping.Scope{Name: ss.Scope().Name(), SchemaURL: ss.SchemaUrl(), ResourceSchemaURL: rs.SchemaUrl()}
			for k := 0; k < ss.Spans().Len(); k++ {
				p.normalizeSpan(ctx, scope, ss.Spans().At(k))
			}
		}
	}
	return td, nil
}

func (p *normalizerProcessor) normalizeResource(ctx context.Context, attrs pcommon.Map) {
	if _, ok := attrs.Get(tenantAttr); !ok && p.cfg.TenantID != "" {
		attrs.PutStr(tenantAttr, p.cfg.TenantID)
	}
	attrs.RemoveIf(func(name string, v pcommon.Value) bool {
		return !p.keepAttribute(ctx, "resource", mapping.UnknownSDK, "", name, v)
	})
}

func (p *normalizerProcessor) normalizeSpan(ctx context.Context, scope mapping.Scope, span ptrace.Span) {
	attrs := span.Attributes()
	traceID := span.TraceID().String()
	sel := p.tables.Select(scope, attrs)
	sdk := sel.SDK()
	if sel.Newer {
		p.tel.newerSchema.add(ctx, sdk, sel.NewerSchemaURL, traceID)
	}

	var kind, operation string
	if sel.Table != nil {
		kind, operation, _ = sel.Table.Kind(span)
		sel.Table.Apply(span)
	}
	if _, ok := attrs.Get(operationAttr); !ok && operation != "" {
		attrs.PutStr(operationAttr, operation)
	}
	kind = p.resolveKind(ctx, kind, attrs, sdk, traceID)

	attrs.RemoveIf(func(name string, v pcommon.Value) bool {
		keep := p.keepAttribute(ctx, "span", sdk, traceID, name, v)
		if keep && sel.Table != nil && !p.isProfileAttr(name) && !sel.Table.Known(name) {
			p.tel.unmapped.add(ctx, sdk, attrLabel(name), traceID)
		}
		return !keep
	})
	p.emit.emit(attrs, kind, sdk)
}

// resolveKind settles aegis.span.kind: the table's kind attribute first, then the
// profile's span kind for gen_ai.operation.name, else chain. An operation that is outside
// the profile, or outside the kind, is kept and counted as invalid.
func (p *normalizerProcessor) resolveKind(ctx context.Context, kind string, attrs pcommon.Map, sdk, traceID string) string {
	op, hasOp := attrs.Get(operationAttr)
	if !hasOp || op.Type() != pcommon.ValueTypeStr {
		if kind == "" {
			return fallbackKind
		}
		return kind
	}
	if kind == "" {
		k, ok := p.profile.KindForOperation(op.Str())
		if !ok {
			p.tel.invalid.add(ctx, sdk, operationAttr, traceID)
			return fallbackKind
		}
		return k
	}
	if !p.profile.OperationAllowed(kind, op.Str()) && kind != fallbackKind {
		p.tel.invalid.add(ctx, sdk, operationAttr, traceID)
	}
	return kind
}

func (p *normalizerProcessor) isProfileAttr(name string) bool {
	_, ok := p.profile.Attribute(name)
	return ok
}

// keepAttribute checks one attribute at scope ("resource" or "span"): profile attributes
// are coerced to their type (or dropped), aegis.* keys outside the profile scope are
// dropped, everything else stays.
func (p *normalizerProcessor) keepAttribute(ctx context.Context, scope, sdk, traceID, name string, v pcommon.Value) bool {
	a, isProfile := p.profile.Attribute(name)
	switch {
	case isProfile && a.Scope == scope:
		coerced, ok := a.Coerce(v)
		if !ok {
			p.tel.invalid.add(ctx, sdk, name, traceID)
			return false
		}
		coerced.CopyTo(v)
		return true
	case strings.HasPrefix(name, aegisPrefix):
		// The aegis namespace is closed: unknown keys and keys in the wrong scope go.
		p.tel.invalid.add(ctx, sdk, name, traceID)
		return false
	default:
		return true
	}
}
