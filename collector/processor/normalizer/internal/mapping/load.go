package mapping

import (
	"bytes"
	"errors"
	"fmt"
	"io/fs"
	"regexp"
	"sort"
	"strings"

	"go.yaml.in/yaml/v3"

	"github.com/arec1b0/traceblame/collector/processor/normalizer/internal/profile"
)

// Attributes the normalizer itself sets on every span; tables may not target them.
var normalizerOwned = map[string]bool{
	"aegis.profile.version":    true,
	"aegis.span.kind":          true,
	"aegis.source.sdk":         true,
	"aegis.normalizer.version": true,
}

var converters = map[string]bool{"": true, "url_host": true, "http_status": true, "class_name": true}

// Load reads every *.yaml table under fsys and checks it against the profile.
func Load(fsys fs.FS, p *profile.Profile) (*Registry, error) {
	sdks, err := sdkEnum(p)
	if err != nil {
		return nil, err
	}
	var tables []*Table
	err = fs.WalkDir(fsys, ".", func(path string, d fs.DirEntry, err error) error {
		if err != nil || d.IsDir() || !strings.HasSuffix(path, ".yaml") {
			return err
		}
		raw, err := fs.ReadFile(fsys, path)
		if err != nil {
			return err
		}
		t, err := decode(raw)
		if err != nil {
			return fmt.Errorf("mapping %s: %w", path, err)
		}
		if err := t.check(p, sdks); err != nil {
			return fmt.Errorf("mapping %s: %w", path, err)
		}
		tables = append(tables, t)
		return nil
	})
	if err != nil {
		return nil, err
	}
	if len(tables) == 0 {
		return nil, errors.New("mapping: no mapping tables")
	}
	return newRegistry(tables, p)
}

func decode(raw []byte) (*Table, error) {
	dec := yaml.NewDecoder(bytes.NewReader(raw))
	dec.KnownFields(true)
	var t Table
	if err := dec.Decode(&t); err != nil {
		return nil, err
	}
	return &t, nil
}

func sdkEnum(p *profile.Profile) (map[string]bool, error) {
	a, ok := p.Attribute("aegis.source.sdk")
	if !ok {
		return nil, fmt.Errorf("mapping: profile %s has no aegis.source.sdk", p.ID)
	}
	out := map[string]bool{}
	for _, v := range a.Enum {
		out[v] = true
	}
	delete(out, UnknownSDK)
	return out, nil
}

func (t *Table) check(p *profile.Profile, sdks map[string]bool) error {
	if !sdks[t.SDK] {
		return fmt.Errorf("sdk %s is not a value of aegis.source.sdk in %s", t.SDK, p.ID)
	}
	if t.Version == "" || t.Source == "" {
		return errors.New("version and source are required")
	}
	if len(t.Match.Scopes)+len(t.Match.ScopePrefixes)+len(t.Match.Markers) == 0 {
		return errors.New("match needs scopes, scope_prefixes or markers")
	}
	if t.SchemaVersion != "" {
		v, ok := parseVersion(t.SchemaVersion)
		if !ok {
			return fmt.Errorf("schema_version %s is not MAJOR.MINOR.PATCH", t.SchemaVersion)
		}
		t.schema = schemaVersion{family: familyCore, version: v}
	}
	if err := t.checkKind(p); err != nil {
		return err
	}
	var err error
	if t.content, err = compile("content", t.ContentAttrs, p); err != nil {
		return err
	}
	if t.known, err = compile("known", t.KnownAttrs, p); err != nil {
		return err
	}
	for name := range t.known {
		if t.isContent(name) {
			return fmt.Errorf("%s is both known and content", name)
		}
	}
	for i := range t.Map {
		r := &t.Map[i]
		if err := checkRule(r, p); err != nil {
			return err
		}
		for _, s := range r.From {
			// JSON sources may read named fields out of a content attribute; an attribute
			// source would copy content into a profile attribute.
			if s.Attr != "" && s.Attr != r.To && (t.isContent(s.Attr) || p.IsForbidden(s.Attr)) {
				return fmt.Errorf("%s: source %s is content", r.To, s.Attr)
			}
		}
	}
	return nil
}

// compile prepares a known or content list: exact names into a set, patterns compiled.
func compile(what string, entries []Known, p *profile.Profile) (map[string]bool, error) {
	names := map[string]bool{}
	for i := range entries {
		k := &entries[i]
		if k.Pattern != "" {
			re, err := regexp.Compile(k.Pattern)
			if err != nil {
				return nil, fmt.Errorf("%s pattern %q: %w", what, k.Pattern, err)
			}
			k.re = re
			continue
		}
		if _, ok := p.Attribute(k.Name); ok {
			return nil, fmt.Errorf("%s %s is a profile attribute", what, k.Name)
		}
		names[k.Name] = true
	}
	return names, nil
}

func (t *Table) checkKind(p *profile.Profile) error {
	if t.KindRule == nil {
		return nil
	}
	if t.KindRule.From == "" {
		return errors.New("kind needs from")
	}
	values := []KindValue{t.KindRule.Default}
	for _, v := range t.KindRule.Values {
		values = append(values, v)
	}
	for _, v := range values {
		if !p.HasKind(v.Kind) {
			return fmt.Errorf("kind %s is not a span kind of %s", v.Kind, p.ID)
		}
		ops := []string{v.Operation}
		for _, op := range v.OperationBySpanName {
			ops = append(ops, op)
		}
		for _, op := range ops {
			if op != "" && !p.OperationAllowed(v.Kind, op) {
				return fmt.Errorf("operation %s is not allowed for kind %s", op, v.Kind)
			}
		}
	}
	return nil
}

func checkRule(r *Rule, p *profile.Profile) error {
	a, ok := p.Attribute(r.To)
	switch {
	case normalizerOwned[r.To]:
		return fmt.Errorf("%s is set by the normalizer", r.To)
	case p.IsForbidden(r.To):
		return fmt.Errorf("%s is forbidden in %s", r.To, p.ID)
	case !ok || a.Scope != "span":
		return fmt.Errorf("%s is not a span attribute of %s", r.To, p.ID)
	case len(r.From) == 0:
		return fmt.Errorf("%s: no sources", r.To)
	}
	for j := range r.From {
		s := &r.From[j]
		kinds := 0
		for _, set := range []string{s.Attr, s.JSON, s.Event} {
			if set != "" {
				kinds++
			}
		}
		if s.StatusMessage {
			kinds++
		}
		if s.Match != "" {
			re, err := regexp.Compile(s.Match)
			if err != nil {
				return fmt.Errorf("%s: match %q: %w", r.To, s.Match, err)
			}
			if re.NumSubexp() != 1 {
				return fmt.Errorf("%s: match %q needs exactly one capture group", r.To, s.Match)
			}
			s.re = re
		}
		switch {
		case kinds != 1:
			return fmt.Errorf("%s: a source needs exactly one of attr, json, event, status_message", r.To)
		case (s.JSON != "" || s.Event != "") && s.Field == "":
			return fmt.Errorf("%s: a json or event source needs a field", r.To)
		case s.Keep && s.Attr == "":
			return fmt.Errorf("%s: keep applies to attr sources only", r.To)
		case !converters[s.Convert]:
			return fmt.Errorf("%s: convert %s is unknown", r.To, s.Convert)
		}
	}
	return nil
}

// Tables returns the tables sorted by ID.
func (r *Registry) Tables() []*Table {
	out := append([]*Table(nil), r.tables...)
	sort.Slice(out, func(i, j int) bool { return out[i].ID() < out[j].ID() })
	return out
}
