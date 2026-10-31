package mapping

import (
	"encoding/json"
	"math"
	"net/url"

	"go.opentelemetry.io/collector/pdata/pcommon"
	"go.opentelemetry.io/collector/pdata/ptrace"
)

// Apply maps the span's attributes in place.
//
// For each rule, in table order: when the span already carries the target under its profile
// name, that value stays (unless a source is the target itself, for value maps). Otherwise
// the first source with a usable value fills the target. Every attr source is consumed,
// i.e. removed, unless it is marked keep. Values are not type-checked here: the processor
// coerces every profile attribute afterwards.
func (t *Table) Apply(span ptrace.Span) {
	attrs := span.Attributes()
	type result struct {
		name  string
		value pcommon.Value
	}
	var results []result // in rule order, so the output order is deterministic
	consumed := map[string]bool{}
	for _, r := range t.Map {
		selfSourced := false
		for _, s := range r.From {
			if s.Attr == "" {
				continue
			}
			if s.Attr == r.To {
				selfSourced = true
			}
			if !s.Keep {
				consumed[s.Attr] = true
			}
		}
		if _, native := attrs.Get(r.To); native && !selfSourced {
			continue
		}
		for _, s := range r.From {
			if v, ok := s.read(span); ok {
				results = append(results, result{r.To, v})
				break
			}
		}
	}
	attrs.RemoveIf(func(name string, _ pcommon.Value) bool { return consumed[name] })
	for _, r := range results {
		r.value.CopyTo(attrs.PutEmpty(r.name))
	}
}

func (s Source) read(span ptrace.Span) (pcommon.Value, bool) {
	var v pcommon.Value
	switch {
	case s.Attr != "":
		got, ok := span.Attributes().Get(s.Attr)
		if !ok {
			return v, false
		}
		v = got
	case s.JSON != "":
		got, ok := span.Attributes().Get(s.JSON)
		if !ok || got.Type() != pcommon.ValueTypeStr {
			return v, false
		}
		field, ok := jsonField(got.Str(), s.Field)
		if !ok {
			return v, false
		}
		v = field
	case s.Event != "":
		if s.OnError && span.Status().Code() != ptrace.StatusCodeError {
			return v, false
		}
		got, ok := eventAttribute(span, s.Event, s.Field)
		if !ok {
			return v, false
		}
		v = got
	}
	out := pcommon.NewValueEmpty()
	v.CopyTo(out)
	if s.Convert == "url_host" {
		if out.Type() != pcommon.ValueTypeStr {
			return out, false
		}
		u, err := url.Parse(out.Str())
		if err != nil || u.Hostname() == "" {
			return out, false
		}
		out.SetStr(u.Hostname())
	}
	if len(s.Values) > 0 {
		mapValues(out, s.Values)
	}
	return out, true
}

// jsonField reads one top-level field of a JSON object; numbers become int when integral.
func jsonField(doc, field string) (pcommon.Value, bool) {
	var obj map[string]any
	if err := json.Unmarshal([]byte(doc), &obj); err != nil {
		return pcommon.Value{}, false
	}
	raw, ok := obj[field]
	if !ok || raw == nil {
		return pcommon.Value{}, false
	}
	v := pcommon.NewValueEmpty()
	switch x := raw.(type) {
	case string:
		v.SetStr(x)
	case bool:
		v.SetBool(x)
	case float64:
		if x == math.Trunc(x) && math.Abs(x) < 1<<53 {
			v.SetInt(int64(x))
		} else {
			v.SetDouble(x)
		}
	default:
		return v, false
	}
	return v, true
}

func eventAttribute(span ptrace.Span, event, field string) (pcommon.Value, bool) {
	events := span.Events()
	for i := 0; i < events.Len(); i++ {
		if events.At(i).Name() != event {
			continue
		}
		if v, ok := events.At(i).Attributes().Get(field); ok {
			return v, true
		}
	}
	return pcommon.Value{}, false
}

func mapValues(v pcommon.Value, values map[string]string) {
	switch v.Type() {
	case pcommon.ValueTypeStr:
		if to, ok := values[v.Str()]; ok {
			v.SetStr(to)
		}
	case pcommon.ValueTypeSlice:
		for i := 0; i < v.Slice().Len(); i++ {
			mapValues(v.Slice().At(i), values)
		}
	default:
	}
}

// Kind derives the span kind, and an operation for spans without gen_ai.operation.name,
// from the table's kind attribute. ok is false when the table has no kind rule or the span
// lacks the attribute; the processor then derives the kind from gen_ai.operation.name.
func (t *Table) Kind(span ptrace.Span) (kind, operation string, ok bool) {
	if t.KindRule == nil {
		return "", "", false
	}
	v, found := span.Attributes().Get(t.KindRule.From)
	if !found {
		return "", "", false
	}
	kv, known := t.KindRule.Values[v.AsString()]
	if !known {
		kv = t.KindRule.Default
	}
	op := kv.Operation
	if byName, ok := kv.OperationBySpanName[span.Name()]; ok {
		op = byName
	}
	return kv.Kind, op, true
}
