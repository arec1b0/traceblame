// Package mapping loads the normalizer's mapping tables (one YAML file per SDK and source
// convention version, ADR-0001) and applies them to spans. The format is documented in
// collector/processor/normalizer/mappings/README.md.
package mapping

import (
	"fmt"
	"regexp"

	"go.yaml.in/yaml/v3"
)

// UnknownSDK is the aegis.source.sdk value of spans that no table matched.
const UnknownSDK = "unknown"

// Table maps the attributes of one SDK and source convention version.
type Table struct {
	SDK           string   `yaml:"sdk"`
	Version       string   `yaml:"version"`
	SchemaVersion string   `yaml:"schema_version"`
	Source        string   `yaml:"source"`
	Match         Match    `yaml:"match"`
	KindRule      *KindMap `yaml:"kind"`
	Map           []Rule   `yaml:"map"`
	KnownAttrs    []Known  `yaml:"known"`
	// ContentAttrs are prompt/completion content: always dropped, never mapped (issue #9).
	ContentAttrs []Known `yaml:"content"`

	schema  schemaVersion
	known   map[string]bool
	content map[string]bool
}

// Match says which spans a table applies to: by instrumentation scope name, then by the
// presence of a marker attribute.
type Match struct {
	Scopes        []string `yaml:"scopes"`
	ScopePrefixes []string `yaml:"scope_prefixes"`
	Markers       []string `yaml:"markers"`
}

// KindMap derives aegis.span.kind from a vendor attribute.
type KindMap struct {
	From    string               `yaml:"from"`
	Values  map[string]KindValue `yaml:"values"`
	Default KindValue            `yaml:"default"`
}

// KindValue is the span kind, and the gen_ai.operation.name for spans without one.
type KindValue struct {
	Kind                string            `yaml:"kind"`
	Operation           string            `yaml:"operation"`
	OperationBySpanName map[string]string `yaml:"operation_by_span_name"`
}

// Rule fills one profile attribute from the first source that has a usable value.
type Rule struct {
	To   string   `yaml:"to"`
	From []Source `yaml:"from"`
}

// Source is one place a value can come from: a span attribute, a field of a JSON object
// held in a span attribute, an attribute of a span event, or the span's status message.
type Source struct {
	Attr          string `yaml:"attr"`
	JSON          string `yaml:"json"`
	Event         string `yaml:"event"`
	StatusMessage bool   `yaml:"status_message"`
	Field         string `yaml:"field"`
	Keep          bool   `yaml:"keep"`
	OnError       bool   `yaml:"on_error"`
	// Match is a regular expression with exactly one capture group; the source yields the
	// group, or nothing when the value does not match.
	Match   string            `yaml:"match"`
	Convert string            `yaml:"convert"`
	Values  map[string]string `yaml:"values"`

	re *regexp.Regexp
}

// Known is an attribute the table passes through without counting it as unmapped: an exact
// name, or a regular expression.
type Known struct {
	Name    string
	Pattern string

	re *regexp.Regexp
}

// UnmarshalYAML accepts a plain name or {pattern: <regexp>}.
func (k *Known) UnmarshalYAML(node *yaml.Node) error {
	if node.Kind == yaml.ScalarNode {
		k.Name = node.Value
		return nil
	}
	var v struct {
		Pattern string `yaml:"pattern"`
	}
	if err := node.Decode(&v); err != nil {
		return err
	}
	if v.Pattern == "" {
		return fmt.Errorf("line %d: known entry needs a name or a pattern", node.Line)
	}
	k.Pattern = v.Pattern
	return nil
}

// ID is "<sdk>/<version>".
func (t *Table) ID() string {
	return t.SDK + "/" + t.Version
}

// Known reports whether the table passes name through without counting it.
func (t *Table) Known(name string) bool {
	return matches(name, t.known, t.KnownAttrs)
}

func (t *Table) isContent(name string) bool {
	return matches(name, t.content, t.ContentAttrs)
}

func matches(name string, names map[string]bool, entries []Known) bool {
	if names[name] {
		return true
	}
	for _, k := range entries {
		if k.re != nil && k.re.MatchString(name) {
			return true
		}
	}
	return false
}
