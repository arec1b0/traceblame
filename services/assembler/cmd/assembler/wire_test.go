package main

import (
	"reflect"
	"testing"
	"time"

	"github.com/arec1b0/traceblame/services/assembler/internal/config"
)

func loaded(t *testing.T, extra map[string]string) config.Config {
	t.Helper()
	env := map[string]string{"POSTGRES_DSN": "postgres://h/db", "ASSEMBLER_OWNER": "r1"}
	for k, v := range extra {
		env[k] = v
	}
	cfg, err := config.Load(func(k string) (string, bool) { v, ok := env[k]; return v, ok })
	if err != nil {
		t.Fatal(err)
	}
	return cfg
}

// A setting that is validated in config but never reaches the poller does nothing in the
// binary: the window floor and its recovery steps were once left out. Every field of the
// poller's configuration must be filled from a loaded configuration.
func TestEveryPollConfigFieldIsFilledFromTheLoadedConfig(t *testing.T) {
	got := reflect.ValueOf(pollConfig(loaded(t, nil)))
	for i := range got.NumField() {
		if got.Field(i).IsZero() {
			t.Errorf("poll.Config.%s is zero: main does not wire it", got.Type().Field(i).Name)
		}
	}
}

func TestThePollConfigCarriesTheValuesThatWereSet(t *testing.T) {
	cfg := loaded(t, map[string]string{
		"ASSEMBLER_MIN_WINDOW": "250ms", "ASSEMBLER_WINDOW_RECOVERY_STEPS": "8",
		"ASSEMBLER_MAX_WINDOW": "90s", "ASSEMBLER_PARTITIONS": "6", "ASSEMBLER_INITIAL_LOOKBACK": "10m",
	})
	p := pollConfig(cfg)
	if p.MinWindow != 250*time.Millisecond || p.WindowRecoverySteps != 8 || p.MaxWindow != 90*time.Second ||
		p.PartitionCount != 6 || p.InitialLookback != 10*time.Minute || p.Owner != "r1" {
		t.Fatalf("%+v", p)
	}
}
