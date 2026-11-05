package main

import (
	"testing"

	"github.com/stretchr/testify/assert"
)

func TestBacklogStatsAreNearestRankMilliseconds(t *testing.T) {
	ns := make([]int64, 0, 100)
	for i := int64(100); i >= 1; i-- {
		ns = append(ns, i*1_000_000) // 1..100 ms, unsorted
	}
	got := backlogStats(ns)
	assert.Equal(t, Backlog{P50: 50, P95: 95, Max: 100}, got)
	assert.Equal(t, int64(100_000_000), ns[0], "the input is not reordered")
	assert.Equal(t, Backlog{}, backlogStats(nil))
	assert.Equal(t, Backlog{P50: 0.5, P95: 0.5, Max: 0.5}, backlogStats([]int64{500_000}))
}
