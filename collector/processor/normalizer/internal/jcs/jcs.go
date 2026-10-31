// Package jcs implements the JSON Canonicalization Scheme, RFC 8785: the canonical form the
// profile's aegis.tool.args_hash is computed over, so that shims and the normalizer agree on
// one hash for one set of arguments.
//
// Input must be I-JSON (RFC 7493): valid UTF-8, no lone surrogates, no duplicate property
// names, numbers within the IEEE 754 double range. Anything else is an error.
package jcs

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math"
	"sort"
	"strconv"
	"strings"
	"unicode/utf16"
	"unicode/utf8"
)

// Canonicalize returns the RFC 8785 serialization of one JSON document.
func Canonicalize(raw []byte) ([]byte, error) {
	if !utf8.Valid(raw) {
		return nil, errors.New("jcs: invalid UTF-8")
	}
	if loneSurrogate(raw) {
		return nil, errors.New("jcs: lone surrogate")
	}
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.UseNumber()
	var out bytes.Buffer
	if err := value(dec, &out); err != nil {
		return nil, err
	}
	if _, err := dec.Token(); !errors.Is(err, io.EOF) {
		return nil, errors.New("jcs: data after the JSON value")
	}
	return out.Bytes(), nil
}

func value(dec *json.Decoder, out *bytes.Buffer) error {
	tok, err := dec.Token()
	if err != nil {
		return fmt.Errorf("jcs: %w", err)
	}
	switch v := tok.(type) {
	case json.Delim:
		if v == '{' {
			return object(dec, out)
		}
		return array(dec, out)
	case string:
		writeString(out, v)
	case json.Number:
		f, err := strconv.ParseFloat(v.String(), 64)
		if err != nil {
			return fmt.Errorf("jcs: number %s: %w", v, err)
		}
		s, err := formatNumber(f)
		if err != nil {
			return err
		}
		out.WriteString(s)
	case bool:
		out.WriteString(strconv.FormatBool(v))
	case nil:
		out.WriteString("null")
	}
	return nil
}

func object(dec *json.Decoder, out *bytes.Buffer) error {
	members := map[string][]byte{}
	var keys []string
	for dec.More() {
		tok, err := dec.Token()
		if err != nil {
			return fmt.Errorf("jcs: %w", err)
		}
		key, _ := tok.(string)
		if _, dup := members[key]; dup {
			return fmt.Errorf("jcs: duplicate property %q", key)
		}
		var v bytes.Buffer
		if err := value(dec, &v); err != nil {
			return err
		}
		members[key] = v.Bytes()
		keys = append(keys, key)
	}
	if _, err := dec.Token(); err != nil { // '}'
		return fmt.Errorf("jcs: %w", err)
	}
	sort.Slice(keys, func(i, j int) bool { return lessUTF16(keys[i], keys[j]) })
	out.WriteByte('{')
	for i, k := range keys {
		if i > 0 {
			out.WriteByte(',')
		}
		writeString(out, k)
		out.WriteByte(':')
		out.Write(members[k])
	}
	out.WriteByte('}')
	return nil
}

func array(dec *json.Decoder, out *bytes.Buffer) error {
	out.WriteByte('[')
	for i := 0; dec.More(); i++ {
		if i > 0 {
			out.WriteByte(',')
		}
		if err := value(dec, out); err != nil {
			return err
		}
	}
	if _, err := dec.Token(); err != nil { // ']'
		return fmt.Errorf("jcs: %w", err)
	}
	out.WriteByte(']')
	return nil
}

func lessUTF16(a, b string) bool {
	x, y := utf16.Encode([]rune(a)), utf16.Encode([]rune(b))
	for i := 0; i < len(x) && i < len(y); i++ {
		if x[i] != y[i] {
			return x[i] < y[i]
		}
	}
	return len(x) < len(y)
}

// writeString follows RFC 8785 section 3.2.2.2 (ECMAScript JSON.stringify).
func writeString(out *bytes.Buffer, s string) {
	out.WriteByte('"')
	for _, r := range s {
		switch r {
		case '"':
			out.WriteString(`\"`)
		case '\\':
			out.WriteString(`\\`)
		case '\b':
			out.WriteString(`\b`)
		case '\t':
			out.WriteString(`\t`)
		case '\n':
			out.WriteString(`\n`)
		case '\f':
			out.WriteString(`\f`)
		case '\r':
			out.WriteString(`\r`)
		default:
			if r < 0x20 {
				fmt.Fprintf(out, `\u%04x`, r)
			} else {
				out.WriteRune(r)
			}
		}
	}
	out.WriteByte('"')
}

// formatNumber serializes like ECMAScript Number.prototype.toString (ECMA-262 7.1.12.1,
// with Note 2): the shortest digits that round-trip, then the ECMAScript layout.
func formatNumber(f float64) (string, error) {
	if math.IsNaN(f) || math.IsInf(f, 0) {
		return "", errors.New("jcs: NaN and Infinity are not JSON numbers")
	}
	if f == 0 {
		return "0", nil // also -0
	}
	sign := ""
	if f < 0 {
		sign, f = "-", -f
	}
	mantissa, exponent, _ := strings.Cut(strconv.FormatFloat(f, 'e', -1, 64), "e")
	digits := strings.Replace(mantissa, ".", "", 1)
	exp, err := strconv.Atoi(exponent)
	if err != nil {
		return "", fmt.Errorf("jcs: %w", err)
	}
	k, n := len(digits), exp+1
	switch {
	case k <= n && n <= 21:
		return sign + digits + strings.Repeat("0", n-k), nil
	case 0 < n && n <= 21:
		return sign + digits[:n] + "." + digits[n:], nil
	case -6 < n && n <= 0:
		return sign + "0." + strings.Repeat("0", -n) + digits, nil
	}
	e := strconv.Itoa(n - 1)
	if n-1 >= 0 {
		e = "+" + e
	}
	if k == 1 {
		return sign + digits + "e" + e, nil
	}
	return sign + digits[:1] + "." + digits[1:] + "e" + e, nil
}

// loneSurrogate reports a \uXXXX escape of a UTF-16 surrogate that is not part of a pair.
// encoding/json would silently replace it with U+FFFD.
func loneSurrogate(raw []byte) bool {
	for i := 0; i < len(raw); i++ {
		if raw[i] != '\\' {
			continue
		}
		if i+1 >= len(raw) {
			return false
		}
		if raw[i+1] != 'u' {
			i++ // an escaped character, e.g. \\ or \"
			continue
		}
		u, ok := hex4(raw, i+2)
		if !ok {
			return false // malformed: the decoder reports it
		}
		switch {
		case u >= 0xd800 && u <= 0xdbff:
			if i+7 < len(raw) && raw[i+6] == '\\' && raw[i+7] == 'u' {
				if lo, ok := hex4(raw, i+8); ok && lo >= 0xdc00 && lo <= 0xdfff {
					i += 11
					continue
				}
			}
			return true
		case u >= 0xdc00 && u <= 0xdfff:
			return true
		}
		i += 5
	}
	return false
}

func hex4(raw []byte, at int) (uint16, bool) {
	if at+4 > len(raw) {
		return 0, false
	}
	n, err := strconv.ParseUint(string(raw[at:at+4]), 16, 16)
	return uint16(n), err == nil
}
