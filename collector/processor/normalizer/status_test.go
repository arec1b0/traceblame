package normalizer

import (
	"testing"

	"github.com/stretchr/testify/assert"
	"go.opentelemetry.io/collector/pdata/ptrace"
)

var errorFixtures = []string{
	"openinference/openinference-instrumentation-openai-0.1.68/chat_error",
	"openllmetry/opentelemetry-instrumentation-openai-0.62.4/chat_error",
	"otel-genai/opentelemetry-instrumentation-openai-v2-2.4b0/chat_error",
	"otel-genai/opentelemetry-instrumentation-openai-v2-2.4b0/chat_error_latest_experimental",
}

// setErrorMessage rewrites every place an SDK puts the client's error message: the status
// message and the exception events' exception.message.
func setErrorMessage(span ptrace.Span, msg string) {
	span.Status().SetMessage(msg)
	for i := 0; i < span.Events().Len(); i++ {
		if ev := span.Events().At(i); ev.Name() == "exception" {
			ev.Attributes().PutStr("exception.message", msg)
		}
	}
}

// The status comes only from the client's "Error code: NNN" prefix, 100-599; no other
// digit in a message is ever taken for one.
func TestHTTPStatusExtraction(t *testing.T) {
	for _, tc := range []struct {
		msg  string
		want int64 // 0: none
	}{
		{"Error code: 429 - {'error': {'message': 'rate limited'}}", 429},
		{"InternalServerError: Error code: 503 - upstream unavailable", 503},
		{"Error code: 400 - max_tokens 4096 > 2048 on port 11434", 400},
		{"Error code: 404", 404},
		{"Error code: 4000 - not a status", 0},
		{"Error code: 099 - below range", 0},
		{"Error code: 600 - above range", 0},
		{"Error code: 42 - too short", 0},
		{"Connection error: http://localhost:11434 refused", 0},
		{"request took 500 ms; upstream status 502", 0},
		{"timeout after 30s, 3 retries", 0},
	} {
		for _, id := range errorFixtures {
			t.Run(tc.msg+"/"+id, func(t *testing.T) {
				td := fixtureByID(t, id).Load(t)
				setErrorMessage(firstSpan(td), tc.msg)
				h := newHarness(t, nil)
				got := firstSpan(h.run(td)).Attributes().AsRaw()
				if tc.want == 0 {
					assert.NotContains(t, got, "http.response.status_code")
				} else {
					assert.Equal(t, tc.want, got["http.response.status_code"])
				}
				assert.Empty(t, h.counts(invalidMetric, "sdk", "attr"))
			})
		}
	}
}

func TestHTTPStatusNeedsAnErrorSpan(t *testing.T) {
	for _, id := range errorFixtures {
		td := fixtureByID(t, id).Load(t)
		span := firstSpan(td)
		span.Status().SetCode(ptrace.StatusCodeOk)
		setErrorMessage(span, "Error code: 400 - but the call succeeded")
		got := firstSpan(newHarness(t, nil).run(td)).Attributes().AsRaw()
		assert.NotContains(t, got, "http.response.status_code", id)
	}
}

func TestEverySDKReportsOneErrorClassSpelling(t *testing.T) {
	for _, id := range errorFixtures {
		got := firstSpan(newHarness(t, nil).run(fixtureByID(t, id).Load(t))).Attributes().AsRaw()
		assert.Equal(t, "BadRequestError", got["error.type"], id)
	}
}
