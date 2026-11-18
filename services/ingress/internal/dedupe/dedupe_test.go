package dedupe

import (
	"context"
	"testing"
	"time"

	"github.com/alicebob/miniredis/v2"
	"github.com/redis/go-redis/v9"
)

func newRedis(t *testing.T) (*Redis, *miniredis.Miniredis) {
	t.Helper()
	mr := miniredis.RunT(t)
	rdb := redis.NewClient(&redis.Options{Addr: mr.Addr()})
	t.Cleanup(func() { _ = rdb.Close() })
	return NewRedis(rdb, time.Hour), mr
}

func TestClaimReleaseAndTTL(t *testing.T) {
	t.Parallel()
	ctx := context.Background()
	d, mr := newRedis(t)

	first, err := d.Claim(ctx, "job:1")
	if err != nil || !first {
		t.Fatalf("first claim: %v %v", first, err)
	}
	if again, _ := d.Claim(ctx, "job:1"); again {
		t.Fatal("second claim must be a duplicate")
	}
	if other, _ := d.Claim(ctx, "job:2"); !other {
		t.Fatal("different key must be claimable")
	}

	if err := d.Release(ctx, "job:1"); err != nil {
		t.Fatal(err)
	}
	if reclaimed, _ := d.Claim(ctx, "job:1"); !reclaimed {
		t.Fatal("released key must be claimable")
	}

	mr.FastForward(2 * time.Hour)
	if expired, _ := d.Claim(ctx, "job:1"); !expired {
		t.Fatal("key must be claimable after TTL")
	}
}
