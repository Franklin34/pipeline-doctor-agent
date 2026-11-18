package redisstreams

import (
	"context"
	"errors"
	"io"
	"log/slog"
	"sync"
	"testing"
	"time"

	"github.com/alicebob/miniredis/v2"
	"github.com/redis/go-redis/v9"

	"github.com/Franklin34/pipeline-doctor-agent/services/ingress/internal/queue"
)

func newQueue(t *testing.T, consumer string, maxAttempts int, rdb *redis.Client) *Queue {
	t.Helper()
	q, err := New(rdb, Config{
		Stream: "pipeline-failed", DLQStream: "pipeline-failed-dlq", Group: "doctor",
		Consumer: consumer, MaxAttempts: maxAttempts,
		MinIdle: 20 * time.Millisecond, BlockTime: 20 * time.Millisecond,
	}, slog.New(slog.NewTextHandler(io.Discard, nil)))
	if err != nil {
		t.Fatal(err)
	}
	return q
}

func setup(t *testing.T) *redis.Client {
	t.Helper()
	mr := miniredis.RunT(t)
	rdb := redis.NewClient(&redis.Options{Addr: mr.Addr()})
	t.Cleanup(func() { _ = rdb.Close() })
	return rdb
}

// run consumes in the background and returns a stop function.
func run(t *testing.T, q *Queue, h queue.Handler) (stop func()) {
	t.Helper()
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() { defer close(done); _ = q.Consume(ctx, h) }()
	return func() { cancel(); <-done }
}

func eventually(t *testing.T, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		if cond() {
			return
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatal("condition not met in time")
}

func TestPublishConsumeAck(t *testing.T) {
	rdb := setup(t)
	q := newQueue(t, "c1", 3, rdb)
	if err := q.Publish(context.Background(), []byte("hello")); err != nil {
		t.Fatal(err)
	}
	var mu sync.Mutex
	var got []queue.Message
	stop := run(t, q, func(_ context.Context, m queue.Message) error {
		mu.Lock()
		defer mu.Unlock()
		got = append(got, m)
		return nil
	})
	defer stop()
	eventually(t, func() bool { mu.Lock(); defer mu.Unlock(); return len(got) == 1 })
	if string(got[0].Body) != "hello" || got[0].Attempt != 1 {
		t.Fatalf("unexpected message %+v", got[0])
	}
	eventually(t, func() bool {
		p, _ := rdb.XPending(context.Background(), "pipeline-failed", "doctor").Result()
		return p.Count == 0
	})
	if n, _ := q.DLQLen(context.Background()); n != 0 {
		t.Fatalf("DLQ should be empty, has %d", n)
	}
}

func TestRetriesThenDeadLetters(t *testing.T) {
	rdb := setup(t)
	q := newQueue(t, "c1", 3, rdb)
	_ = q.Publish(context.Background(), []byte("poison"))

	var mu sync.Mutex
	var attempts []int
	stop := run(t, q, func(_ context.Context, m queue.Message) error {
		mu.Lock()
		defer mu.Unlock()
		attempts = append(attempts, m.Attempt)
		return errors.New("boom")
	})
	defer stop()

	eventually(t, func() bool { n, _ := q.DLQLen(context.Background()); return n == 1 })
	stop()

	mu.Lock()
	defer mu.Unlock()
	if len(attempts) != 3 || attempts[0] != 1 || attempts[1] != 2 || attempts[2] != 3 {
		t.Fatalf("want attempts [1 2 3], got %v", attempts)
	}
	dlq, _ := rdb.XRange(context.Background(), "pipeline-failed-dlq", "-", "+").Result()
	if len(dlq) != 1 || dlq[0].Values["data"] != "poison" || dlq[0].Values["attempts"] != "3" ||
		dlq[0].Values["error"] != "boom" {
		t.Fatalf("unexpected DLQ entry: %+v", dlq)
	}
	if p, _ := rdb.XPending(context.Background(), "pipeline-failed", "doctor").Result(); p.Count != 0 {
		t.Fatalf("original must be acked after dead-lettering, pending=%d", p.Count)
	}
}

func TestSucceedsOnRetry(t *testing.T) {
	rdb := setup(t)
	q := newQueue(t, "c1", 3, rdb)
	_ = q.Publish(context.Background(), []byte("flaky"))

	var mu sync.Mutex
	calls := 0
	stop := run(t, q, func(_ context.Context, _ queue.Message) error {
		mu.Lock()
		defer mu.Unlock()
		calls++
		if calls < 2 {
			return errors.New("transient")
		}
		return nil
	})
	defer stop()
	eventually(t, func() bool {
		p, _ := rdb.XPending(context.Background(), "pipeline-failed", "doctor").Result()
		mu.Lock()
		defer mu.Unlock()
		return calls == 2 && p.Count == 0
	})
	if n, _ := q.DLQLen(context.Background()); n != 0 {
		t.Fatal("must not dead-letter a message that eventually succeeds")
	}
}

// A consumer that read a message and died leaves it pending; another consumer
// reclaims it. If it already used up its attempts it goes to the DLQ unprocessed.
func TestReclaimsFromCrashedConsumer(t *testing.T) {
	rdb := setup(t)
	ctx := context.Background()
	dead := newQueue(t, "dead", 3, rdb)
	_ = dead.Publish(ctx, []byte("orphan"))
	_ = rdb.XGroupCreateMkStream(ctx, "pipeline-failed", "doctor", "0").Err()
	// "dead" reads the message and never acks.
	if _, err := rdb.XReadGroup(ctx, &redis.XReadGroupArgs{
		Group: "doctor", Consumer: "dead", Streams: []string{"pipeline-failed", ">"}, Count: 1,
	}).Result(); err != nil {
		t.Fatal(err)
	}

	live := newQueue(t, "live", 3, rdb)
	var mu sync.Mutex
	var got []queue.Message
	stop := run(t, live, func(_ context.Context, m queue.Message) error {
		mu.Lock()
		defer mu.Unlock()
		got = append(got, m)
		return nil
	})
	defer stop()
	eventually(t, func() bool { mu.Lock(); defer mu.Unlock(); return len(got) == 1 })
	if got[0].Attempt != 2 {
		t.Fatalf("reclaimed message should be attempt 2, got %d", got[0].Attempt)
	}
}

func TestConfigValidation(t *testing.T) {
	t.Parallel()
	base := Config{Stream: "s", DLQStream: "d", Group: "g", Consumer: "c",
		MaxAttempts: 3, MinIdle: time.Second, BlockTime: time.Second}
	tests := []struct {
		name   string
		mutate func(*Config)
	}{
		{"no stream", func(c *Config) { c.Stream = "" }},
		{"zero attempts", func(c *Config) { c.MaxAttempts = 0 }},
		{"zero idle", func(c *Config) { c.MinIdle = 0 }},
	}
	if _, err := New(nil, base, nil); err != nil {
		t.Fatalf("base config should be valid: %v", err)
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			t.Parallel()
			c := base
			tt.mutate(&c)
			if _, err := New(nil, c, nil); err == nil {
				t.Fatal("expected validation error")
			}
		})
	}
}
