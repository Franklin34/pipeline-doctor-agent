// Package redisstreams implements queue.Queue on Redis Streams with consumer
// groups, per-message retry counting and a dead-letter stream.
//
// Retry protocol: a message that is delivered but not acknowledged stays in the
// group's pending list. Redis counts deliveries per message (XPENDING). After a
// visibility timeout (MinIdle) XAUTOCLAIM hands it to a consumer again. When the
// delivery count reaches MaxAttempts the message is copied to the DLQ stream
// (with the last error) and acknowledged.
package redisstreams

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"strconv"
	"strings"
	"time"

	"github.com/redis/go-redis/v9"

	"github.com/Franklin34/pipeline-doctor-agent/services/ingress/internal/queue"
)

const dataField = "data"

// Config configures the adapter.
type Config struct {
	Stream      string
	DLQStream   string
	Group       string
	Consumer    string
	MaxAttempts int
	MinIdle     time.Duration // visibility timeout before a pending message is reclaimed
	BlockTime   time.Duration // how long a read waits for new messages
	MaxLen      int64         // approximate stream trim length (0 = no trimming)
}

func (c Config) validate() error {
	switch {
	case c.Stream == "" || c.DLQStream == "" || c.Group == "" || c.Consumer == "":
		return errors.New("redisstreams: stream, dlq stream, group and consumer are required")
	case c.MaxAttempts < 1:
		return errors.New("redisstreams: MaxAttempts must be >= 1")
	case c.MinIdle <= 0 || c.BlockTime <= 0:
		return errors.New("redisstreams: MinIdle and BlockTime must be positive")
	}
	return nil
}

// Queue is the Redis Streams adapter.
type Queue struct {
	rdb redis.UniversalClient
	cfg Config
	log *slog.Logger
}

var _ queue.Queue = (*Queue)(nil)

// New validates cfg and returns a Queue.
func New(rdb redis.UniversalClient, cfg Config, log *slog.Logger) (*Queue, error) {
	if err := cfg.validate(); err != nil {
		return nil, err
	}
	if log == nil {
		log = slog.Default()
	}
	return &Queue{rdb: rdb, cfg: cfg, log: log}, nil
}

// Publish appends body to the stream.
func (q *Queue) Publish(ctx context.Context, body []byte) error {
	args := &redis.XAddArgs{Stream: q.cfg.Stream, Values: map[string]any{dataField: body}}
	if q.cfg.MaxLen > 0 {
		args.MaxLen, args.Approx = q.cfg.MaxLen, true
	}
	if err := q.rdb.XAdd(ctx, args).Err(); err != nil {
		return fmt.Errorf("redisstreams: publish: %w", err)
	}
	return nil
}

// Ping checks Redis connectivity.
func (q *Queue) Ping(ctx context.Context) error { return q.rdb.Ping(ctx).Err() }

// DLQLen returns the number of dead-lettered messages (for alerting).
func (q *Queue) DLQLen(ctx context.Context) (int64, error) {
	return q.rdb.XLen(ctx, q.cfg.DLQStream).Result()
}

// Consume runs the read loop until ctx is cancelled.
func (q *Queue) Consume(ctx context.Context, h queue.Handler) error {
	err := q.rdb.XGroupCreateMkStream(ctx, q.cfg.Stream, q.cfg.Group, "0").Err()
	if err != nil && !strings.HasPrefix(err.Error(), "BUSYGROUP") {
		return fmt.Errorf("redisstreams: create group: %w", err)
	}
	for ctx.Err() == nil {
		// Reclaimed messages first: they have already waited the longest.
		if err := q.processReclaimed(ctx, h); err != nil && ctx.Err() == nil {
			q.log.Error("reclaim failed", "error", err)
			sleep(ctx, q.cfg.BlockTime)
		}
		if err := q.processNew(ctx, h); err != nil && ctx.Err() == nil {
			q.log.Error("read failed", "error", err)
			sleep(ctx, q.cfg.BlockTime)
		}
	}
	return ctx.Err()
}

func (q *Queue) processNew(ctx context.Context, h queue.Handler) error {
	res, err := q.rdb.XReadGroup(ctx, &redis.XReadGroupArgs{
		Group: q.cfg.Group, Consumer: q.cfg.Consumer,
		Streams: []string{q.cfg.Stream, ">"}, Count: 10, Block: q.cfg.BlockTime,
	}).Result()
	if errors.Is(err, redis.Nil) {
		return nil // timed out, nothing new
	}
	if err != nil {
		return err
	}
	for _, s := range res {
		for _, m := range s.Messages {
			q.handle(ctx, h, m, 1)
		}
	}
	return nil
}

func (q *Queue) processReclaimed(ctx context.Context, h queue.Handler) error {
	msgs, _, err := q.rdb.XAutoClaim(ctx, &redis.XAutoClaimArgs{
		Stream: q.cfg.Stream, Group: q.cfg.Group, Consumer: q.cfg.Consumer,
		MinIdle: q.cfg.MinIdle, Start: "0-0", Count: 10,
	}).Result()
	if err != nil {
		return err
	}
	for _, m := range msgs {
		attempts, err := q.attempts(ctx, m.ID)
		if err != nil {
			return err
		}
		if attempts > q.cfg.MaxAttempts {
			// A previous consumer died mid-processing; do not run it again.
			q.deadLetter(ctx, m, attempts, "exceeded max attempts (consumer may have crashed)")
			continue
		}
		q.handle(ctx, h, m, attempts)
	}
	return nil
}

func (q *Queue) handle(ctx context.Context, h queue.Handler, m redis.XMessage, attempt int) {
	body, _ := m.Values[dataField].(string)
	err := h(ctx, queue.Message{ID: m.ID, Body: []byte(body), Attempt: attempt})
	if err == nil {
		if err := q.rdb.XAck(ctx, q.cfg.Stream, q.cfg.Group, m.ID).Err(); err != nil {
			q.log.Error("ack failed; message will be redelivered", "id", m.ID, "error", err)
		}
		return
	}
	if attempt >= q.cfg.MaxAttempts {
		q.deadLetter(ctx, m, attempt, err.Error())
		return
	}
	q.log.Warn("handler failed; will retry", "id", m.ID, "attempt", attempt, "error", err)
}

// attempts returns Redis' delivery count for id.
func (q *Queue) attempts(ctx context.Context, id string) (int, error) {
	p, err := q.rdb.XPendingExt(ctx, &redis.XPendingExtArgs{
		Stream: q.cfg.Stream, Group: q.cfg.Group, Start: id, End: id, Count: 1,
	}).Result()
	if err != nil {
		return 0, err
	}
	if len(p) == 0 {
		return 0, fmt.Errorf("redisstreams: message %s is not pending", id)
	}
	return int(p[0].RetryCount), nil
}

// deadLetter copies the message to the DLQ and acknowledges the original. A
// crash between the two steps can duplicate a DLQ entry, which is harmless.
func (q *Queue) deadLetter(ctx context.Context, m redis.XMessage, attempts int, reason string) {
	if len(reason) > 500 {
		reason = reason[:500]
	}
	body, _ := m.Values[dataField].(string)
	err := q.rdb.XAdd(ctx, &redis.XAddArgs{Stream: q.cfg.DLQStream, Values: map[string]any{
		dataField:   body,
		"source_id": m.ID,
		"attempts":  strconv.Itoa(attempts),
		"error":     reason,
		"failed_at": time.Now().UTC().Format(time.RFC3339),
	}}).Err()
	if err != nil {
		q.log.Error("dead-letter write failed; message stays pending", "id", m.ID, "error", err)
		return
	}
	if err := q.rdb.XAck(ctx, q.cfg.Stream, q.cfg.Group, m.ID).Err(); err != nil {
		q.log.Error("ack after dead-letter failed", "id", m.ID, "error", err)
	}
	q.log.Error("message moved to DLQ", "id", m.ID, "attempts", attempts, "reason", reason)
}

func sleep(ctx context.Context, d time.Duration) {
	t := time.NewTimer(d)
	defer t.Stop()
	select {
	case <-ctx.Done():
	case <-t.C:
	}
}
