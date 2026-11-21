// Package pubsub is the Google Cloud Pub/Sub publisher for the ingress.
//
// The ingress only publishes; the Python worker pulls. Redelivery, retry limits and the dead-letter
// topic are configured on the subscription (see deploy/terraform), not in application code: Pub/Sub
// counts delivery attempts and forwards a message that keeps failing.
package pubsub

import (
	"context"
	"errors"
	"fmt"
	"time"

	"cloud.google.com/go/pubsub/v2"
	"cloud.google.com/go/pubsub/v2/apiv1/pubsubpb"
)

// publishTimeout bounds one publish, so a stalled Pub/Sub cannot hold a webhook open; the server
// then answers 503 and Jenkins retries.
const publishTimeout = 8 * time.Second

// Publisher sends event bodies to one topic.
type Publisher struct {
	client    *pubsub.Client
	pub       *pubsub.Publisher
	topicName string
}

// New connects to project and publishes to topic. With PUBSUB_EMULATOR_HOST set (tests, local
// development) the client talks to the emulator without credentials.
func New(ctx context.Context, project, topic string) (*Publisher, error) {
	if project == "" || topic == "" {
		return nil, errors.New("pubsub: project and topic are required")
	}
	client, err := pubsub.NewClient(ctx, project)
	if err != nil {
		return nil, fmt.Errorf("pubsub: client: %w", err)
	}
	return NewWithClient(client, project, topic), nil
}

// NewWithClient is for tests that bring their own client.
func NewWithClient(client *pubsub.Client, project, topic string) *Publisher {
	pub := client.Publisher(topic)
	pub.PublishSettings.Timeout = publishTimeout
	return &Publisher{client: client, pub: pub, topicName: fmt.Sprintf("projects/%s/topics/%s", project, topic)}
}

// Publish sends body and waits for the server to confirm it: a webhook is acknowledged only after
// the event is durably queued.
func (p *Publisher) Publish(ctx context.Context, body []byte) error {
	if _, err := p.pub.Publish(ctx, &pubsub.Message{Data: body}).Get(ctx); err != nil {
		return fmt.Errorf("pubsub: publish: %w", err)
	}
	return nil
}

// Ping checks that the topic exists and is reachable (used for readiness).
func (p *Publisher) Ping(ctx context.Context) error {
	if _, err := p.client.TopicAdminClient.GetTopic(ctx, &pubsubpb.GetTopicRequest{Topic: p.topicName}); err != nil {
		return fmt.Errorf("pubsub: topic: %w", err)
	}
	return nil
}

// Close flushes pending messages and releases the connection.
func (p *Publisher) Close() error {
	p.pub.Stop()
	return p.client.Close()
}
