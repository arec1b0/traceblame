package main

import (
	"github.com/arec1b0/traceblame/services/assembler/internal/config"
	"github.com/arec1b0/traceblame/services/assembler/internal/poll"
)

// pollConfig maps the loaded configuration onto the poller's. Kept in one place, with a test
// that every field is filled, so a setting cannot be validated in config and then not applied.
func pollConfig(cfg config.Config) poll.Config {
	return poll.Config{
		Owner:               cfg.Owner,
		PartitionCount:      cfg.PartitionCount,
		Grace:               cfg.Grace,
		MaxWindow:           cfg.MaxWindow,
		MinWindow:           cfg.MinWindow,
		WindowRecoverySteps: cfg.WindowRecoverySteps,
		LeaseTTL:            cfg.LeaseTTL,
		BatchSize:           cfg.BatchSize,
		Lookback:            cfg.Lookback,
		InitialLookback:     cfg.InitialLookback,
		PollInterval:        cfg.PollInterval,
		AssemblerVersion:    cfg.AssemblerVersion,
	}
}
