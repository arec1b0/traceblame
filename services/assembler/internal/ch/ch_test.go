package ch

import (
	"strings"
	"testing"
)

func TestTheEmbeddedStatementsAreTheContractsAndHaveTheirPlaceholders(t *testing.T) {
	for name, tc := range map[string]struct {
		sql  string
		want []string
	}{
		"scan":      {scanSQL, []string{"{w0:Int64}", "{w1:Int64}", "{partition_count:UInt32}", "{partition:UInt32}", "FROM trace_arrivals"}},
		"spans":     {spansSQL, []string{"{trace_ids:Array(String)}", "{since:Int64}", "FROM spans"}},
		"assembled": {assembledSQL, []string{"{trace_ids:Array(String)}", "FROM trace_summary"}},
	} {
		t.Run(name, func(t *testing.T) {
			for _, w := range tc.want {
				if !strings.Contains(tc.sql, w) {
					t.Errorf("missing %q in\n%s", w, tc.sql)
				}
			}
		})
	}
}

func TestSpansReadHasNoServiceFilter(t *testing.T) {
	if strings.Contains(spansSQL, "service IN") || strings.Contains(spansSQL, "service =") {
		t.Fatal("ADR-0003 amendment: the assembly read must not filter by service")
	}
}

func TestIdArrayAcceptsOnlyTraceIDs(t *testing.T) {
	got, err := idArray([]string{"0af7651916cd43dd8448eb211c80319c", "b7ad6b7169203331b7ad6b7169203331"})
	if err != nil || got != "['0af7651916cd43dd8448eb211c80319c','b7ad6b7169203331b7ad6b7169203331']" {
		t.Fatalf("got %q, %v", got, err)
	}
	for _, bad := range []string{"", "x", "0AF7651916CD43DD8448EB211C80319C", "a'] OR 1=1 --", strings.Repeat("a", 33)} {
		if _, err := idArray([]string{bad}); err == nil {
			t.Errorf("%q must be rejected", bad)
		}
	}
}
