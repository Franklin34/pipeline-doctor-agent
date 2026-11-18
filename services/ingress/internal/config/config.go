// Package config loads service configuration from the environment.
package config

import (
	"errors"
	"fmt"
	"strconv"
	"time"
)

// Config holds all runtime settings.
type Config struct {
	Addr            string
	WebhookSecret   []byte
	RedisURL        string
	Stream          string
	IdempotencyTTL  time.Duration
	OTLPEndpoint    string // empty disables trace export
	MaxBodyBytes    int64
	ShutdownTimeout time.Duration
	StreamMaxLen    int64
}

// Load reads configuration using getenv (os.Getenv in production) so tests
// can inject values.
func Load(getenv func(string) string) (Config, error) {
	str := func(key, def string) string {
		if v := getenv(key); v != "" {
			return v
		}
		return def
	}
	c := Config{
		Addr:            str("INGRESS_ADDR", ":8080"),
		WebhookSecret:   []byte(getenv("WEBHOOK_SHARED_SECRET")),
		RedisURL:        str("REDIS_URL", "redis://localhost:6379/0"),
		Stream:          str("QUEUE_STREAM", "pipeline-failed"),
		OTLPEndpoint:    getenv("OTEL_EXPORTER_OTLP_ENDPOINT"),
		MaxBodyBytes:    1 << 20,
		ShutdownTimeout: 10 * time.Second,
		StreamMaxLen:    100_000,
	}
	var err error
	if c.IdempotencyTTL, err = time.ParseDuration(str("IDEMPOTENCY_TTL", "24h")); err != nil {
		return Config{}, fmt.Errorf("IDEMPOTENCY_TTL: %w", err)
	}
	if c.IdempotencyTTL <= 0 {
		return Config{}, errors.New("IDEMPOTENCY_TTL must be positive")
	}
	if v := getenv("MAX_BODY_BYTES"); v != "" {
		if c.MaxBodyBytes, err = strconv.ParseInt(v, 10, 64); err != nil || c.MaxBodyBytes < 1 {
			return Config{}, errors.New("MAX_BODY_BYTES must be a positive integer")
		}
	}
	if len(c.WebhookSecret) < 16 {
		return Config{}, errors.New("WEBHOOK_SHARED_SECRET must be set (min 16 bytes)")
	}
	return c, nil
}
