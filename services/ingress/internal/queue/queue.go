// Package queue defines the transport-agnostic queue interface. Adapters:
// redisstreams (local dev) and, in Phase 10, Google Cloud Pub/Sub.
//
// Delivery is at-least-once: consumers MUST be idempotent.
package queue

import "context"

// Message is one delivery of a queued payload.
type Message struct {
	ID      string // transport-assigned identifier
	Body    []byte
	Attempt int // 1 on first delivery, incremented on each redelivery
}

// Handler processes a message. Returning nil acknowledges it; returning an
// error leaves it for redelivery until the adapter's max attempts, after which
// it is moved to the dead-letter queue.
type Handler func(ctx context.Context, m Message) error

// Queue is the minimal contract shared by all adapters.
type Queue interface {
	Publish(ctx context.Context, body []byte) error
	// Consume blocks, delivering messages to h until ctx is cancelled.
	Consume(ctx context.Context, h Handler) error
	// Ping checks the transport is reachable (used for readiness).
	Ping(ctx context.Context) error
}
