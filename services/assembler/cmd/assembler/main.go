// Command assembler is the trace-assembler (ADR-0003): it polls the ClickHouse arrival
// watermark, writes trace_summary and publishes trace.completed.
package main

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"sync"
	"sync/atomic"
	"syscall"
	"time"

	"github.com/prometheus/client_golang/prometheus/promhttp"

	"github.com/arec1b0/traceblame/services/assembler/internal/ch"
	"github.com/arec1b0/traceblame/services/assembler/internal/config"
	"github.com/arec1b0/traceblame/services/assembler/internal/metrics"
	"github.com/arec1b0/traceblame/services/assembler/internal/natsx"
	"github.com/arec1b0/traceblame/services/assembler/internal/poll"
	"github.com/arec1b0/traceblame/services/assembler/internal/reconcile"
	"github.com/arec1b0/traceblame/services/assembler/internal/store"
)

func main() {
	log := slog.New(slog.NewJSONHandler(os.Stderr, nil)).With("component", "assembler", "version", config.Version)
	if err := run(log); err != nil {
		log.Error("assembler stopped", "error", err.Error())
		os.Exit(1)
	}
}

func run(log *slog.Logger) error {
	cfg, err := config.Load(os.LookupEnv)
	if err != nil {
		return err
	}
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	click, err := ch.Open(ctx, ch.Options{
		Addr: cfg.ClickHouseAddr, Database: cfg.ClickHouseDatabase,
		User: cfg.ClickHouseUser, Password: cfg.ClickHousePassword, TLS: cfg.ClickHouseTLS,
	})
	if err != nil {
		return err
	}
	defer func() { _ = click.Close() }()

	pool, err := store.Open(ctx, cfg.PostgresDSN)
	if err != nil {
		return err
	}
	defer pool.Close()

	nc, js, err := natsx.Connect(cfg.NATSURL, cfg.NATSUser, cfg.NATSPassword)
	if err != nil {
		return err
	}
	defer nc.Close()
	if err := natsx.EnsureStream(ctx, js, natsx.StreamConfig{
		Name: cfg.Stream, Subject: cfg.Subject, DuplicateWindow: cfg.DuplicateWindow,
		MaxAge: cfg.StreamMaxAge, Replicas: cfg.StreamReplicas,
	}); err != nil {
		return err
	}

	m := metrics.New(cfg.Grace)
	pub := natsx.NewPublisher(js, cfg.Subject, config.PublishRetryBudget)
	pub.OnDuplicate = m.Duplicate
	loop := poll.New(poll.Config{
		Owner: cfg.Owner, PartitionCount: cfg.PartitionCount, Grace: cfg.Grace,
		MaxWindow: cfg.MaxWindow, LeaseTTL: cfg.LeaseTTL, BatchSize: cfg.BatchSize,
		Lookback: cfg.Lookback, PollInterval: cfg.PollInterval, AssemblerVersion: cfg.AssemblerVersion,
	}, m.Source(click), click, pub, store.New(store.PoolDB{Pool: pool}, cfg.LeaseTTL)).
		WithMetrics(m).WithLogger(log)

	var ready atomic.Bool
	srv := serveMetrics(cfg.MetricsAddr, m, &ready, log)

	var wg sync.WaitGroup
	wg.Add(1)
	go func() {
		defer wg.Done()
		reconcile.Run(ctx, click, m, cfg.ReconcileEvery, cfg.ReconcileMargin, cfg.Lookback, log)
	}()

	ready.Store(true)
	log.Info("assembler started", "partitions", cfg.PartitionCount, "owner", cfg.Owner)
	runErr := loop.Run(ctx)
	ready.Store(false)

	stop()
	wg.Wait()
	shutdown, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := srv.Shutdown(shutdown); err != nil && !errors.Is(err, http.ErrServerClosed) {
		log.Warn("metrics server shutdown", "error", err.Error())
	}
	return runErr
}

func serveMetrics(addr string, m *metrics.Metrics, ready *atomic.Bool, log *slog.Logger) *http.Server {
	mux := http.NewServeMux()
	mux.Handle("/metrics", promhttp.HandlerFor(m.Registry, promhttp.HandlerOpts{}))
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, _ *http.Request) { _, _ = fmt.Fprintln(w, "ok") })
	mux.HandleFunc("/readyz", func(w http.ResponseWriter, _ *http.Request) {
		if !ready.Load() {
			http.Error(w, "not ready", http.StatusServiceUnavailable)
			return
		}
		_, _ = fmt.Fprintln(w, "ok")
	})
	srv := &http.Server{Addr: addr, Handler: mux, ReadHeaderTimeout: 5 * time.Second}
	go func() {
		if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Error("metrics server", "error", err.Error())
		}
	}()
	return srv
}
