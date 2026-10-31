package jcs

import (
	"math"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// RFC 8785, Appendix B: ECMAScript-compatible number serialization samples.
func TestNumbersFollowAppendixB(t *testing.T) {
	for _, tc := range []struct {
		bits uint64
		want string
	}{
		{0x0000000000000000, "0"},
		{0x8000000000000000, "0"},
		{0x0000000000000001, "5e-324"},
		{0x8000000000000001, "-5e-324"},
		{0x7fefffffffffffff, "1.7976931348623157e+308"},
		{0xffefffffffffffff, "-1.7976931348623157e+308"},
		{0x4340000000000000, "9007199254740992"},
		{0xc340000000000000, "-9007199254740992"},
		{0x4430000000000000, "295147905179352830000"},
		{0x44b52d02c7e14af5, "9.999999999999997e+22"},
		{0x44b52d02c7e14af6, "1e+23"},
		{0x44b52d02c7e14af7, "1.0000000000000001e+23"},
		{0x444b1ae4d6e2ef4e, "999999999999999700000"},
		{0x444b1ae4d6e2ef4f, "999999999999999900000"},
		{0x444b1ae4d6e2ef50, "1e+21"},
		{0x3eb0c6f7a0b5ed8c, "9.999999999999997e-7"},
		{0x3eb0c6f7a0b5ed8d, "0.000001"},
		{0x41b3de4355555553, "333333333.3333332"},
		{0x41b3de4355555554, "333333333.33333325"},
		{0x41b3de4355555555, "333333333.3333333"},
		{0x41b3de4355555556, "333333333.3333334"},
		{0x41b3de4355555557, "333333333.33333343"},
		{0xbecbf647612f3696, "-0.0000033333333333333333"},
		{0x43143ff3c1cb0959, "1424953923781206.2"},
	} {
		got, err := formatNumber(math.Float64frombits(tc.bits))
		require.NoError(t, err)
		assert.Equal(t, tc.want, got, "%016x", tc.bits)
	}
	for _, bits := range []uint64{0x7fffffffffffffff, 0x7ff0000000000000} {
		_, err := formatNumber(math.Float64frombits(bits))
		assert.Error(t, err, "NaN and Infinity are not JSON")
	}
}

// RFC 8785, sections 3.2.2 and 3.2.3: the sample object and its canonical form.
func TestTheRFCSample(t *testing.T) {
	in := `{
       "numbers": [333333333.33333329, 1E30, 4.50,
                   2e-3, 0.000000000000000000000000001],
       "string": "\u20ac$\u000F\u000aA'\u0042\u0022\u005c\\\"\/",
       "literals": [null, true, false]
     }`
	got, err := Canonicalize([]byte(in))
	require.NoError(t, err)
	assert.Equal(t,
		`{"literals":[null,true,false],"numbers":[333333333.3333333,1e+30,4.5,0.002,1e-27],"string":"€$\u000f\nA'B\"\\\\\"/"}`,
		string(got))
}

// RFC 8785, section 3.2.3: properties sort by UTF-16 code units.
func TestPropertiesSortByUTF16CodeUnits(t *testing.T) {
	in := `{
       "\u20ac": "Euro Sign",
       "\r": "Carriage Return",
       "\ufb33": "Hebrew Letter Dalet With Dagesh",
       "1": "One",
       "\ud83d\ude00": "Emoji: Grinning Face",
       "\u0080": "Control",
       "\u00f6": "Latin Small Letter O With Diaeresis"
     }`
	got, err := Canonicalize([]byte(in))
	require.NoError(t, err)
	assert.Equal(t, "{\"\\r\":\"Carriage Return\",\"1\":\"One\",\"\u0080\":\"Control\","+
		"\"\u00f6\":\"Latin Small Letter O With Diaeresis\",\"\u20ac\":\"Euro Sign\","+
		"\"\U0001F600\":\"Emoji: Grinning Face\",\"\ufb33\":\"Hebrew Letter Dalet With Dagesh\"}", string(got))
}

func TestNestedObjectsSortAndArraysKeepTheirOrder(t *testing.T) {
	got, err := Canonicalize([]byte(`{"b": [3, {"z": 1, "a": 2}], "a": {"d": [], "c": {}}}`))
	require.NoError(t, err)
	assert.Equal(t, `{"a":{"c":{},"d":[]},"b":[3,{"a":2,"z":1}]}`, string(got))
}

func TestRejectsWhatIJSONForbids(t *testing.T) {
	for name, in := range map[string]string{
		"duplicate property":  `{"a": 1, "a": 2}`,
		"lone high surrogate": `{"a": "\ud83d"}`,
		"lone low surrogate":  `["\ude00x"]`,
		"invalid UTF-8":       "[\"\xff\"]",
		"number out of range": `[1e400]`,
		"trailing data":       `{} {}`,
		"not JSON":            `{"city":`,
	} {
		_, err := Canonicalize([]byte(in))
		assert.Error(t, err, name)
	}
}

func TestEscapedBackslashBeforeUIsNotASurrogate(t *testing.T) {
	got, err := Canonicalize([]byte(`["\\ud83d"]`))
	require.NoError(t, err)
	assert.Equal(t, `["\\ud83d"]`, string(got))
}
