package mapping

import (
	"strconv"
	"strings"
)

const schemaURLPrefix = "https://opentelemetry.io/schemas/"

type family int

const (
	familyCore family = iota
	// GenAI conventions moved to semantic-conventions-genai after core 1.41.1 (ADR-0001),
	// so its dev line is newer than every core release.
	familyGenAIDev
)

type version struct {
	major, minor, patch int
	pre                 string
}

func parseVersion(s string) (version, bool) {
	core, pre, _ := strings.Cut(s, "-")
	parts := strings.Split(core, ".")
	if len(parts) != 3 {
		return version{}, false
	}
	var n [3]int
	for i, p := range parts {
		v, err := strconv.Atoi(p)
		if err != nil || v < 0 {
			return version{}, false
		}
		n[i] = v
	}
	return version{major: n[0], minor: n[1], patch: n[2], pre: pre}, true
}

type schemaVersion struct {
	family  family
	version version
}

// parseSchemaURL reads an OpenTelemetry schema URL: .../schemas/<version> or
// .../schemas/gen-ai-dev/<version>.
func parseSchemaURL(url string) (schemaVersion, bool) {
	rest, ok := strings.CutPrefix(url, schemaURLPrefix)
	if !ok {
		return schemaVersion{}, false
	}
	f := familyCore
	if v, ok := strings.CutPrefix(rest, "gen-ai-dev/"); ok {
		f, rest = familyGenAIDev, v
	}
	v, ok := parseVersion(rest)
	if !ok {
		return schemaVersion{}, false
	}
	return schemaVersion{family: f, version: v}, true
}

// compare returns -1, 0 or 1. A pre-release sorts before its release.
func (a schemaVersion) compare(b schemaVersion) int {
	if a.family != b.family {
		return sign(int(a.family) - int(b.family))
	}
	x, y := a.version, b.version
	for _, d := range []int{x.major - y.major, x.minor - y.minor, x.patch - y.patch} {
		if d != 0 {
			return sign(d)
		}
	}
	switch {
	case x.pre == y.pre:
		return 0
	case x.pre == "":
		return 1
	case y.pre == "":
		return -1
	default:
		return sign(strings.Compare(x.pre, y.pre))
	}
}

func sign(d int) int {
	switch {
	case d < 0:
		return -1
	case d > 0:
		return 1
	default:
		return 0
	}
}
