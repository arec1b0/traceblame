package poll

import (
	"sort"
	"strings"
)

// Assemble builds the summary of one trace from its spans; ok is false when no span is a root.
// The root gives the service, ids, status, start and duration. The tenant, bundle digest and
// session id come from the root, else from the earliest span that has one. revisionMs is the
// trace's last arrival.
func Assemble(version string, revisionMs int64, spans []Span) (s Summary, ok bool) {
	if len(spans) == 0 {
		return Summary{}, false
	}
	ordered := make([]Span, len(spans))
	copy(ordered, spans)
	sort.SliceStable(ordered, func(i, j int) bool { return ordered[i].StartMicros < ordered[j].StartMicros })

	var root *Span
	for i := range ordered {
		if ordered[i].ParentSpanID == "" {
			root = &ordered[i]
			break
		}
	}
	if root == nil {
		return Summary{}, false
	}

	s = Summary{
		TraceID:          root.TraceID,
		AssemblerVersion: version,
		RevisionMs:       revisionMs,
		Service:          root.Service,
		RootSpanID:       root.SpanID,
		RootStatusCode:   root.StatusCode,
		StartMicros:      root.StartMicros,
		DurationMs:       root.DurationMs,
		SpanCount:        uint32(len(ordered)), //nolint:gosec // a trace has far fewer than 2^32 spans; the column is UInt32
	}
	s.TenantID = first(root.TenantID, ordered, func(sp Span) string { return sp.TenantID })
	s.BundleDigest = first(root.BundleDigest, ordered, func(sp Span) string { return sp.BundleDigest })
	s.SessionID = first(root.SessionID, ordered, func(sp Span) string { return sp.SessionID })
	for _, sp := range ordered {
		if isError(sp.StatusCode) {
			s.ErrorSpanCount++
		}
	}
	return s, true
}

// first is the root's value, else the first non-empty value in start order.
func first(rootValue string, ordered []Span, get func(Span) string) string {
	if rootValue != "" {
		return rootValue
	}
	for _, sp := range ordered {
		if v := get(sp); v != "" {
			return v
		}
	}
	return ""
}

func isError(status string) bool {
	return strings.EqualFold(status, "error") || strings.EqualFold(status, "status_code_error")
}
