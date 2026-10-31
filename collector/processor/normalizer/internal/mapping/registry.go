package mapping

import (
	"fmt"
	"sort"
	"strings"

	"go.opentelemetry.io/collector/pdata/pcommon"

	"github.com/arec1b0/traceblame/collector/processor/normalizer/internal/profile"
)

// Registry holds every table and picks the one for a span (ADR-0001, "Normalizer behaviour
// on a newer schema_url").
type Registry struct {
	profile *profile.Profile
	tables  []*Table
	bySDK   map[string][]*Table // sorted by schema version, oldest first
	sdks    []string            // match order
}

// Scope is what selection reads from the span's instrumentation scope and resource.
type Scope struct {
	Name              string
	SchemaURL         string
	ResourceSchemaURL string
}

// Selection is the table chosen for a span.
type Selection struct {
	Table *Table // nil when no table matched
	// Newer is set when the span's schema_url is newer than every table of its SDK; the
	// newest table is used and NewerSchemaURL names the URL.
	Newer          bool
	NewerSchemaURL string
}

// SDK is the aegis.source.sdk value of the selection.
func (s Selection) SDK() string {
	if s.Table == nil {
		return UnknownSDK
	}
	return s.Table.SDK
}

func newRegistry(tables []*Table, p *profile.Profile) (*Registry, error) {
	r := &Registry{profile: p, tables: tables, bySDK: map[string][]*Table{}}
	for _, t := range tables {
		r.bySDK[t.SDK] = append(r.bySDK[t.SDK], t)
	}
	for sdk, ts := range r.bySDK {
		r.sdks = append(r.sdks, sdk)
		if len(ts) == 1 {
			continue
		}
		for _, t := range ts {
			if t.SchemaVersion == "" {
				return nil, fmt.Errorf("mapping: %s has %d tables: each needs a schema_version", sdk, len(ts))
			}
		}
		sort.Slice(ts, func(i, j int) bool { return ts[i].schema.compare(ts[j].schema) < 0 })
		for i := 1; i < len(ts); i++ {
			if ts[i].schema.compare(ts[i-1].schema) == 0 {
				return nil, fmt.Errorf("mapping: %s has two tables for schema_version %s", sdk, ts[i].SchemaVersion)
			}
		}
	}
	sort.Strings(r.sdks)
	return r, nil
}

// Select picks the table for a span: by scope name first, then by marker attribute, then
// by schema_url among the tables of that SDK.
func (r *Registry) Select(scope Scope, attrs pcommon.Map) Selection {
	sdk := r.matchScope(scope.Name)
	if sdk == "" {
		sdk = r.matchMarkers(attrs)
	}
	if sdk == "" {
		return Selection{}
	}
	tables := r.bySDK[sdk]
	newest := tables[len(tables)-1]
	if newest.SchemaVersion == "" {
		return Selection{Table: newest}
	}
	url := scope.SchemaURL
	if url == "" {
		url = scope.ResourceSchemaURL
	}
	v, ok := parseSchemaURL(url)
	if !ok {
		return Selection{Table: newest}
	}
	if v.compare(newest.schema) > 0 {
		return Selection{Table: newest, Newer: true, NewerSchemaURL: url}
	}
	chosen := tables[0]
	for _, t := range tables {
		if t.schema.compare(v) <= 0 {
			chosen = t
		}
	}
	return Selection{Table: chosen}
}

func (r *Registry) matchScope(name string) string {
	for _, sdk := range r.sdks {
		for _, t := range r.bySDK[sdk] {
			for _, s := range t.Match.Scopes {
				if name == s {
					return sdk
				}
			}
			for _, p := range t.Match.ScopePrefixes {
				if strings.HasPrefix(name, p) {
					return sdk
				}
			}
		}
	}
	return ""
}

func (r *Registry) matchMarkers(attrs pcommon.Map) string {
	for _, sdk := range r.sdks {
		for _, t := range r.bySDK[sdk] {
			for _, m := range t.Match.Markers {
				if _, ok := attrs.Get(m); ok {
					return sdk
				}
			}
		}
	}
	return ""
}

// IsContent reports whether name is prompt/completion content: a forbidden attribute of the
// profile, or content in any table. It applies to every span, whatever its SDK, so content
// rules fail closed.
func (r *Registry) IsContent(name string) bool {
	if r.profile.IsForbidden(name) {
		return true
	}
	for _, t := range r.tables {
		if t.isContent(name) {
			return true
		}
	}
	return false
}
