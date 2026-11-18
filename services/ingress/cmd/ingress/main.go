// Command ingress receives Jenkins failure webhooks and enqueues them.
package main

import (
	"context"
	"errors"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/redis/go-redis/v9"

	"github.com/Franklin34/pipeline-doctor-agent/services/ingress/internal/config"
	"github.com/Franklin34/pipeline-doctor-agent/services/ingress/internal/dedupe"
	"github.com/Franklin34/pipeline-doctor-agent/services/ingress/internal/queue/redisstreams"
	"github.com/Franklin34/pipeline-doctor-agent/services/ingress/internal/server"
	"github.com/Franklin34/pipeline-doctor-agent/services/ingress/internal/telemetry"
)

func main() {
	log := slog.New(slog.NewJSONHandler(os.Stdout, nil))
	if err := run(log); err != nil {
		log.Error("fatal", "error", err)
		os.Exit(1)
	}
}

func run(log *slog.Logger) error {
	cfg, err := config.Load(os.Getenv)
	if err != nil {
		return err
	}

	// Cancelled on SIGTERM/SIGINT; drives graceful shutdown.
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
	defer stop()

	shutdownTracing, err := telemetry.Init(ctx, cfg.OTLPEndpoint, "pipeline-doctor-ingress")
	if err != nil {
		return err
	}

	opts, err := redis.ParseURL(cfg.RedisURL)
	if err != nil {
		return err
	}
	rdb := redis.NewClient(opts)
	defer func() { _ = rdb.Close() }()

	q, err := redisstreams.New(rdb, redisstreams.Config{
		Stream: cfg.Stream, DLQStream: cfg.Stream + "-dlq", Group: "pipeline-doctor",
		Consumer: "ingress", MaxAttempts: 1, MinIdle: time.Minute, BlockTime: time.Second,
		MaxLen: cfg.StreamMaxLen,
	}, log) // ingress only publishes; consumer settings are unused here
	if err != nil {
		return err
	}

	srv := &http.Server{
		Addr: cfg.Addr,
		Handler: server.New(server.Deps{
			Secret: cfg.WebhookSecret, MaxBodyBytes: cfg.MaxBodyBytes,
			Publisher: q, Dedupe: dedupe.NewRedis(rdb, cfg.IdempotencyTTL),
			Metrics: server.NewMetrics(), Log: log,
		}),
		ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout:       10 * time.Second,
		WriteTimeout:      10 * time.Second,
		IdleTimeout:       60 * time.Second,
	}

	errc := make(chan error, 1)
	go func() {
		log.Info("listening", "addr", cfg.Addr)
		errc <- srv.ListenAndServe()
	}()

	select {
	case err := <-errc:
		return err
	case <-ctx.Done():
		log.Info("shutting down")
	}

	// Stop accepting, let in-flight requests finish, then flush traces.
	shutCtx, cancel := context.WithTimeout(context.Background(), cfg.ShutdownTimeout)
	defer cancel()
	if err := srv.Shutdown(shutCtx); err != nil && !errors.Is(err, http.ErrServerClosed) {
		return err
	}
	return shutdownTracing(shutCtx)
}
