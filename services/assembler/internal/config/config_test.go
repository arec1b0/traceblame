package config

import (
	"strings"
	"testing"
	"time"
)

func env(kv map[string]string) func(string) (string, bool) {
	return func(k string) (string, bool) { v, ok := kv[k]; return v, ok }
}

var base = map[string]string{"POSTGRES_DSN": "postgres://h/db", "ASSEMBLER_OWNER": "r1"}

func with(extra map[string]string) map[string]string {
	m := map[string]string{}
	for k, v := range base {
		m[k] = v
	}
	for k, v := range extra {
		m[k] = v
	}
	return m
}

func TestDefaultsAreTheADRStartingHypotheses(t *testing.T) {
	c, err := Load(env(base))
	if err != nil {
		t.Fatal(err)
	}
	if c.Grace != 5*time.Second || c.PollInterval != 2*time.Second || c.MaxWindow != time.Minute {
		t.Fatalf("grace %s poll %s max window %s", c.Grace, c.PollInterval, c.MaxWindow)
	}
	if c.PartitionCount != 2 || c.LeaseTTL != 30*time.Second || c.DuplicateWindow != time.Hour {
		t.Fatalf("%+v", c)
	}
	if c.AssemblerVersion != Version {
		t.Fatalf("version %q", c.AssemblerVersion)
	}
}

func TestEverythingIsOverridable(t *testing.T) {
	c, err := Load(env(with(map[string]string{
		"ASSEMBLER_PARTITIONS": "4", "ASSEMBLER_GRACE": "8s", "ASSEMBLER_POLL_INTERVAL": "1s",
		"ASSEMBLER_BATCH_SIZE": "50", "ASSEMBLER_DUPLICATE_WINDOW": "2h", "CLICKHOUSE_TLS": "true",
	})))
	if err != nil {
		t.Fatal(err)
	}
	if c.PartitionCount != 4 || c.Grace != 8*time.Second || c.PollInterval != time.Second ||
		c.BatchSize != 50 || c.DuplicateWindow != 2*time.Hour || !c.ClickHouseTLS {
		t.Fatalf("%+v", c)
	}
}

func TestABadValueFailsAtStartupAndNamesTheVariable(t *testing.T) {
	for name, tc := range map[string]struct {
		env  map[string]string
		want string
	}{
		"not an integer":             {with(map[string]string{"ASSEMBLER_PARTITIONS": "two"}), "ASSEMBLER_PARTITIONS"},
		"not a duration":             {with(map[string]string{"ASSEMBLER_GRACE": "5"}), "ASSEMBLER_GRACE"},
		"zero partitions":            {with(map[string]string{"ASSEMBLER_PARTITIONS": "0"}), "must be >= 1"},
		"lease shorter than 3 polls": {with(map[string]string{"ASSEMBLER_LEASE_TTL": "4s"}), "3 poll intervals"},
		"no postgres":                {map[string]string{"ASSEMBLER_OWNER": "r1"}, "POSTGRES_DSN is required"},
		"wildcard subject":           {with(map[string]string{"ASSEMBLER_SUBJECT": "trace.>"}), "not a plain subject"},
		"lookback under window":      {with(map[string]string{"ASSEMBLER_LOOKBACK": "10s"}), "ASSEMBLER_LOOKBACK"},
	} {
		t.Run(name, func(t *testing.T) {
			_, err := Load(env(tc.env))
			if err == nil || !strings.Contains(err.Error(), tc.want) {
				t.Fatalf("want an error with %q, got %v", tc.want, err)
			}
		})
	}
}

func TestTheDuplicateWindowMustOutlastTheLongestRepublish(t *testing.T) {
	_, err := Load(env(with(map[string]string{"ASSEMBLER_DUPLICATE_WINDOW": "1m"})))
	if err == nil || !strings.Contains(err.Error(), "ASSEMBLER_DUPLICATE_WINDOW") {
		t.Fatalf("a 1 minute window is shorter than lease TTL + poll + retry budget: %v", err)
	}
	// the boundary: exactly lease TTL + poll interval + retry budget is enough
	need := 30*time.Second + 2*time.Second + PublishRetryBudget
	if _, err := Load(env(with(map[string]string{"ASSEMBLER_DUPLICATE_WINDOW": need.String()}))); err != nil {
		t.Fatalf("exactly enough must pass: %v", err)
	}
}

func TestTheStreamKeepsMessagesAtLeastAsLongAsItDeduplicates(t *testing.T) {
	_, err := Load(env(with(map[string]string{"ASSEMBLER_STREAM_MAX_AGE": "10m"})))
	if err == nil || !strings.Contains(err.Error(), "ASSEMBLER_STREAM_MAX_AGE") {
		t.Fatalf("%v", err)
	}
}

func TestTheInitialLookbackIsSeparateFromTheSpansLookback(t *testing.T) {
	c, err := Load(env(base))
	if err != nil {
		t.Fatal(err)
	}
	if c.InitialLookback != 48*time.Hour || c.Lookback != 48*time.Hour {
		t.Fatalf("initial %s lookback %s", c.InitialLookback, c.Lookback)
	}
	c, err = Load(env(with(map[string]string{"ASSEMBLER_INITIAL_LOOKBACK": "10m", "ASSEMBLER_LOOKBACK": "720h"})))
	if err != nil || c.InitialLookback != 10*time.Minute || c.Lookback != 720*time.Hour {
		t.Fatalf("%+v %v", c, err)
	}
	if _, err := Load(env(with(map[string]string{"ASSEMBLER_INITIAL_LOOKBACK": "0s"}))); err == nil ||
		!strings.Contains(err.Error(), "ASSEMBLER_INITIAL_LOOKBACK") {
		t.Fatalf("a zero initial lookback must be refused: %v", err)
	}
}

func TestTheWindowShrinksToAFloorThatIsConfigurable(t *testing.T) {
	c, err := Load(env(base))
	if err != nil || c.MinWindow != time.Second || c.WindowRecoverySteps != 5 {
		t.Fatalf("%+v %v", c, err)
	}
	c, err = Load(env(with(map[string]string{"ASSEMBLER_MIN_WINDOW": "250ms", "ASSEMBLER_WINDOW_RECOVERY_STEPS": "8"})))
	if err != nil || c.MinWindow != 250*time.Millisecond || c.WindowRecoverySteps != 8 {
		t.Fatalf("%+v %v", c, err)
	}
	for name, kv := range map[string]map[string]string{
		"floor above the maximum": {"ASSEMBLER_MIN_WINDOW": "2m"},
		"floor of zero":           {"ASSEMBLER_MIN_WINDOW": "0s"},
		"no recovery steps":       {"ASSEMBLER_WINDOW_RECOVERY_STEPS": "0"},
	} {
		if _, err := Load(env(with(kv))); err == nil {
			t.Errorf("%s must be refused", name)
		}
	}
}

func TestAllErrorsAreReportedTogether(t *testing.T) {
	_, err := Load(env(map[string]string{"ASSEMBLER_PARTITIONS": "x", "ASSEMBLER_GRACE": "y"}))
	if err == nil || !strings.Contains(err.Error(), "ASSEMBLER_PARTITIONS") || !strings.Contains(err.Error(), "ASSEMBLER_GRACE") {
		t.Fatalf("%v", err)
	}
}
