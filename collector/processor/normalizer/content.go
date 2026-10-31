package normalizer

import (
	"crypto/sha256"
	"encoding/hex"
	"strings"

	"go.opentelemetry.io/collector/pdata/pcommon"
	"go.opentelemetry.io/collector/pdata/ptrace"

	"github.com/arec1b0/traceblame/collector/processor/normalizer/internal/jcs"
)

const (
	toolArgsAttr     = "gen_ai.tool.call.arguments"
	argsHashAttr     = "aegis.tool.args_hash"
	exceptionEvent   = "exception"
	exceptionTypeKey = "exception.type"
)

// redact removes prompt/completion content from a span (issue #9, ADR-0002). It runs after
// the mapping, so json and event sources have been read, and before the attribute check.
//
//   - Tool call arguments become aegis.tool.args_hash (the profile's algorithm) first.
//   - Content attributes are dropped on every span, its events and its links, whatever the
//     SDK: the profile's forbidden list plus every table's content rules (fail closed).
//   - On GenAI spans, error details go too: every event but exception, every exception
//     attribute but exception.type, and the status message. Provider and tool errors can
//     echo inputs; detectors need only the status code and error.type.
func (p *normalizerProcessor) redact(span ptrace.Span, genAI bool) {
	attrs := span.Attributes()
	if args, ok := attrs.Get(toolArgsAttr); ok {
		if _, has := attrs.Get(argsHashAttr); !has {
			attrs.PutStr(argsHashAttr, argsHash(args.AsString()))
		}
	}
	isContent := func(name string, _ pcommon.Value) bool { return p.tables.IsContent(name) }
	attrs.RemoveIf(isContent)
	for i := 0; i < span.Links().Len(); i++ {
		span.Links().At(i).Attributes().RemoveIf(isContent)
	}
	if !genAI {
		for i := 0; i < span.Events().Len(); i++ {
			span.Events().At(i).Attributes().RemoveIf(isContent)
		}
		return
	}
	span.Status().SetMessage("")
	span.Events().RemoveIf(func(e ptrace.SpanEvent) bool { return e.Name() != exceptionEvent })
	for i := 0; i < span.Events().Len(); i++ {
		span.Events().At(i).Attributes().RemoveIf(func(name string, _ pcommon.Value) bool {
			return name != exceptionTypeKey
		})
	}
}

// argsHash is the profile's aegis.tool.args_hash: the first 16 lowercase hex chars of
// SHA-256 over the RFC 8785 form of the arguments, or over their raw UTF-8 bytes when they
// are not I-JSON.
func argsHash(args string) string {
	b := []byte(args)
	if canonical, err := jcs.Canonicalize(b); err == nil {
		b = canonical
	}
	sum := sha256.Sum256(b)
	return hex.EncodeToString(sum[:])[:16]
}

// isGenAI: the span came from a GenAI SDK, or carries GenAI semconv attributes.
func isGenAI(tableMatched bool, attrs pcommon.Map) bool {
	if tableMatched {
		return true
	}
	found := false
	attrs.Range(func(name string, _ pcommon.Value) bool {
		found = strings.HasPrefix(name, "gen_ai.")
		return !found
	})
	return found
}

// valueBytes is the size of a string value, or of all strings in an array; other types
// count as 0 (the size guard is for text).
func valueBytes(v pcommon.Value) int {
	switch v.Type() {
	case pcommon.ValueTypeStr:
		return len(v.Str())
	case pcommon.ValueTypeSlice:
		n := 0
		for i := 0; i < v.Slice().Len(); i++ {
			n += valueBytes(v.Slice().At(i))
		}
		return n
	default:
		return 0
	}
}
