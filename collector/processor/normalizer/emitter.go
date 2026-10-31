package normalizer

import "go.opentelemetry.io/collector/pdata/pcommon"

// emitter writes the attributes a profile major owns (ADR-0001: one emitter per major).
// The mapping tables produce the canonical attributes; today they are the v1 names, so the
// v1 emitter only stamps the span.
type emitter interface {
	emit(attrs pcommon.Map, kind, sdk string)
}

var emitters = map[string]emitter{
	"aegis-telemetry/1": emitterV1{},
}

type emitterV1 struct{}

func (emitterV1) emit(attrs pcommon.Map, kind, sdk string) {
	attrs.PutStr("aegis.profile.version", "aegis-telemetry/1")
	attrs.PutStr("aegis.span.kind", kind)
	attrs.PutStr("aegis.source.sdk", sdk)
	attrs.PutStr("aegis.normalizer.version", Version)
}
