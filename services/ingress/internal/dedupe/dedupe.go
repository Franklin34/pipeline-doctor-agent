// Package dedupe provides idempotency-key deduplication backed by Redis.
package dedupe

import (
	"context"
	"time"

	"github.com/redis/go-redis/v9"
)

// Redis claims keys with SET NX and a TTL.
type Redis struct {
	rdb    redis.Cmdable
	prefix string
	ttl    time.Duration
}

// NewRedis returns a Deduper using rdb.
func NewRedis(rdb redis.Cmdable, ttl time.Duration) *Redis {
	return &Redis{rdb: rdb, prefix: "ingress:dedupe:", ttl: ttl}
}

// Claim returns true if this caller is the first to see key within the TTL.
func (r *Redis) Claim(ctx context.Context, key string) (bool, error) {
	return r.rdb.SetNX(ctx, r.prefix+key, "1", r.ttl).Result()
}

// Release forgets key so a retry can claim it again. Used when publishing
// fails after a successful Claim.
func (r *Redis) Release(ctx context.Context, key string) error {
	return r.rdb.Del(ctx, r.prefix+key).Err()
}

// Ping checks connectivity (for readiness).
func (r *Redis) Ping(ctx context.Context) error { return r.rdb.Ping(ctx).Err() }
