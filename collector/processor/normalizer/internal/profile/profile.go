// Package profile reads the aegis-telemetry profile that the normalizer embeds: a
// byte-identical copy of schemas/profile/aegis-telemetry.v<major>.yaml, kept in sync by
// `traceblame-contracts gen-schema` and checked by `make contract`.
//
// Only what the normalizer needs is read: attribute names, scopes, types and value
// constraints, the forbidden attributes, and the operations of each span kind.
package profile

import (
	"errors"
	"fmt"
	"math"
	"regexp"
	"strconv"

	"go.opentelemetry.io/collector/pdata/pcommon"
	"go.yaml.in/yaml/v3"
)

// Attribute is one profile attribute.
type Attribute struct {
	Name    string
	Scope   string // resource | span
	Type    string // string | int | double | boolean | string[] | int[] | double[] | boolean[]
	Enum    []string
	Const   string
	pattern *regexp.Regexp
}

// Profile is one profile major.
type Profile struct {
	ID       string // aegis-telemetry/<major>
	Revision string

	attributes      map[string]*Attribute
	forbidden       map[string]string
	operations      map[string]map[string]bool // kind -> allowed gen_ai.operation.name values
	kindByOperation map[string]string
}

type document struct {
	Profile   string `yaml:"profile"`
	Major     int    `yaml:"major"`
	Revision  string `yaml:"revision"`
	SpanKinds map[string]struct {
		Operations []string `yaml:"operations"`
	} `yaml:"span_kinds"`
	Attributes []struct {
		Name    string   `yaml:"name"`
		Scope   string   `yaml:"scope"`
		Type    string   `yaml:"type"`
		Enum    []string `yaml:"enum"`
		Const   string   `yaml:"const"`
		Pattern string   `yaml:"pattern"`
	} `yaml:"attributes"`
	Forbidden []struct {
		Name   string `yaml:"name"`
		Reason string `yaml:"reason"`
	} `yaml:"forbidden"`
}

// Parse reads a profile YAML document.
func Parse(raw []byte) (*Profile, error) {
	var doc document
	if err := yaml.Unmarshal(raw, &doc); err != nil {
		return nil, fmt.Errorf("profile: %w", err)
	}
	if doc.Profile == "" || doc.Major < 1 {
		return nil, errors.New("profile: missing profile id or major")
	}
	p := &Profile{
		ID:              fmt.Sprintf("%s/%d", doc.Profile, doc.Major),
		Revision:        doc.Revision,
		attributes:      map[string]*Attribute{},
		forbidden:       map[string]string{},
		operations:      map[string]map[string]bool{},
		kindByOperation: map[string]string{},
	}
	for kind, spec := range doc.SpanKinds {
		p.operations[kind] = map[string]bool{}
		for _, op := range spec.Operations {
			p.operations[kind][op] = true
			p.kindByOperation[op] = kind
		}
	}
	for _, a := range doc.Attributes {
		attr := &Attribute{Name: a.Name, Scope: a.Scope, Type: a.Type, Enum: a.Enum, Const: a.Const}
		if a.Pattern != "" {
			re, err := regexp.Compile(a.Pattern)
			if err != nil {
				return nil, fmt.Errorf("profile: %s: pattern: %w", a.Name, err)
			}
			attr.pattern = re
		}
		p.attributes[a.Name] = attr
	}
	for _, f := range doc.Forbidden {
		p.forbidden[f.Name] = f.Reason
	}
	return p, nil
}

// Attribute returns the profile attribute with that name.
func (p *Profile) Attribute(name string) (*Attribute, bool) {
	a, ok := p.attributes[name]
	return a, ok
}

// IsForbidden reports whether the attribute must never appear on a normalized span.
func (p *Profile) IsForbidden(name string) bool {
	_, ok := p.forbidden[name]
	return ok
}

// Forbidden returns the names of the forbidden attributes.
func (p *Profile) Forbidden() []string {
	out := make([]string, 0, len(p.forbidden))
	for name := range p.forbidden {
		out = append(out, name)
	}
	return out
}

// HasKind reports whether kind is a span kind of the profile.
func (p *Profile) HasKind(kind string) bool {
	_, ok := p.operations[kind]
	return ok
}

// KindForOperation returns the span kind whose operations include op.
func (p *Profile) KindForOperation(op string) (string, bool) {
	kind, ok := p.kindByOperation[op]
	return kind, ok
}

// OperationAllowed reports whether op is an allowed gen_ai.operation.name for kind.
func (p *Profile) OperationAllowed(kind, op string) bool {
	return p.operations[kind][op]
}

// Coerce converts v to the attribute's type where that loses nothing, then checks the
// attribute's enum, const and pattern. It returns false when v cannot be repaired.
//
// Lossless conversions: a decimal string or an integral double to int; an int or a numeric
// string to double; an int to its decimal string; a scalar to a one-element array; array
// elements by the scalar rules.
func (a *Attribute) Coerce(v pcommon.Value) (pcommon.Value, bool) {
	out := pcommon.NewValueEmpty()
	switch a.Type {
	case "string[]", "int[]", "double[]", "boolean[]":
		elem := a.Type[:len(a.Type)-2]
		slice := out.SetEmptySlice()
		if v.Type() != pcommon.ValueTypeSlice {
			if !coerceScalar(elem, v, slice.AppendEmpty()) {
				return out, false
			}
			return out, a.valid(out)
		}
		for i := 0; i < v.Slice().Len(); i++ {
			if !coerceScalar(elem, v.Slice().At(i), slice.AppendEmpty()) {
				return out, false
			}
		}
		return out, a.valid(out)
	default:
		if !coerceScalar(a.Type, v, out) {
			return out, false
		}
		return out, a.valid(out)
	}
}

func coerceScalar(typ string, v, out pcommon.Value) bool {
	switch typ {
	case "string":
		switch v.Type() {
		case pcommon.ValueTypeStr:
			out.SetStr(v.Str())
		case pcommon.ValueTypeInt:
			out.SetStr(strconv.FormatInt(v.Int(), 10))
		default:
			return false
		}
	case "int":
		switch v.Type() {
		case pcommon.ValueTypeInt:
			out.SetInt(v.Int())
		case pcommon.ValueTypeStr:
			n, err := strconv.ParseInt(v.Str(), 10, 64)
			if err != nil {
				return false
			}
			out.SetInt(n)
		case pcommon.ValueTypeDouble:
			d := v.Double()
			if d != math.Trunc(d) || d > math.MaxInt64 || d < math.MinInt64 {
				return false
			}
			out.SetInt(int64(d))
		default:
			return false
		}
	case "double":
		switch v.Type() {
		case pcommon.ValueTypeDouble:
			out.SetDouble(v.Double())
		case pcommon.ValueTypeInt:
			out.SetDouble(float64(v.Int()))
		case pcommon.ValueTypeStr:
			d, err := strconv.ParseFloat(v.Str(), 64)
			if err != nil {
				return false
			}
			out.SetDouble(d)
		default:
			return false
		}
	case "boolean":
		if v.Type() != pcommon.ValueTypeBool {
			return false
		}
		out.SetBool(v.Bool())
	default:
		return false
	}
	return true
}

func (a *Attribute) valid(v pcommon.Value) bool {
	if v.Type() != pcommon.ValueTypeStr {
		return true
	}
	s := v.Str()
	if a.Const != "" && s != a.Const {
		return false
	}
	if a.pattern != nil && !a.pattern.MatchString(s) {
		return false
	}
	if len(a.Enum) > 0 {
		for _, e := range a.Enum {
			if s == e {
				return true
			}
		}
		return false
	}
	return true
}
