package poll

import "testing"

func span(id, parent, service, status string, start int64, dur float64) Span {
	return Span{
		TraceID: "t", SpanID: id, ParentSpanID: parent, Service: service, StatusCode: status,
		StartMicros: start, DurationMs: dur,
	}
}

func TestAssembleTakesTheRootsFactsAndCountsEverySpan(t *testing.T) {
	spans := []Span{
		span("c2", "r", "tools", "Error", 1_500_000, 40),
		span("r", "", "agent", "Ok", 1_000_000, 900),
		span("c1", "r", "llm", "Ok", 1_200_000, 100),
	}
	spans[1].TenantID, spans[1].BundleDigest, spans[1].SessionID = "tenant-eval", "sha256:b", "s-1"

	got, ok := Assemble("1.0.0", 4242, spans)
	if !ok {
		t.Fatal("a trace with a root must assemble")
	}
	want := Summary{
		TraceID: "t", AssemblerVersion: "1.0.0", RevisionMs: 4242, Service: "agent",
		TenantID: "tenant-eval", RootSpanID: "r", RootStatusCode: "Ok", StartMicros: 1_000_000,
		DurationMs: 900, SpanCount: 3, ErrorSpanCount: 1, BundleDigest: "sha256:b", SessionID: "s-1",
	}
	if got != want {
		t.Fatalf("summary\n got %+v\nwant %+v", got, want)
	}
}

func TestAssembleWithoutARootIsNotASummary(t *testing.T) {
	if _, ok := Assemble("1.0.0", 1, []Span{span("c", "r", "llm", "Ok", 1, 1)}); ok {
		t.Fatal("no root span: nothing to summarise")
	}
	if _, ok := Assemble("1.0.0", 1, nil); ok {
		t.Fatal("no spans: nothing to summarise")
	}
}

func TestAssembleFillsTenantBundleAndSessionFromTheEarliestSpanThatHasThem(t *testing.T) {
	spans := []Span{
		span("r", "", "agent", "Ok", 1_000, 10),
		span("b", "r", "llm", "Ok", 3_000, 1),
		span("a", "r", "llm", "Ok", 2_000, 1),
	}
	spans[1].SessionID, spans[2].SessionID = "late", "early"
	spans[1].TenantID, spans[2].TenantID = "t-late", "t-early"

	got, _ := Assemble("1.0.0", 1, spans)
	if got.SessionID != "early" || got.TenantID != "t-early" {
		t.Fatalf("want the earliest span's values, got session %q tenant %q", got.SessionID, got.TenantID)
	}
}

func TestAssemblePicksTheEarliestRootWhenThereAreSeveral(t *testing.T) {
	spans := []Span{span("late", "", "a", "Ok", 5_000, 1), span("first", "", "b", "Ok", 2_000, 1)}
	got, _ := Assemble("1.0.0", 1, spans)
	if got.RootSpanID != "first" || got.Service != "b" {
		t.Fatalf("got root %q service %q", got.RootSpanID, got.Service)
	}
}

func TestAssembleCountsErrorsWhateverTheCaseOfTheStatus(t *testing.T) {
	spans := []Span{
		span("r", "", "a", "Unset", 1, 1),
		span("1", "r", "a", "Error", 2, 1),
		span("2", "r", "a", "ERROR", 3, 1),
		span("3", "r", "a", "STATUS_CODE_ERROR", 4, 1),
		span("4", "r", "a", "Ok", 5, 1),
	}
	got, _ := Assemble("1.0.0", 1, spans)
	if got.ErrorSpanCount != 3 {
		t.Fatalf("error spans = %d, want 3", got.ErrorSpanCount)
	}
}

func TestEventMsgIDIsTheTraceVersionAndRevision(t *testing.T) {
	e := Event{TraceID: "abc", Revision: 17, SpanCount: 2, AssemblerVersion: "1.0.0"}
	if got, want := e.MsgID(), "abc/1.0.0/17"; got != want {
		t.Fatalf("MsgID = %q, want %q", got, want)
	}
}
