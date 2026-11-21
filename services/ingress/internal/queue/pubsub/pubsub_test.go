package pubsub

import (
	"context"
	"testing"
	"time"

	"cloud.google.com/go/pubsub/v2"
	"cloud.google.com/go/pubsub/v2/apiv1/pubsubpb"
	"cloud.google.com/go/pubsub/v2/pstest"
	"google.golang.org/api/option"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
)

const project, topic = "demo-project", "pipeline-failed"

// fake starts an in-process Pub/Sub server and returns a client connected to it.
func fake(t *testing.T, createTopic bool) (*pstest.Server, *pubsub.Client) {
	t.Helper()
	srv := pstest.NewServer()
	t.Cleanup(func() { _ = srv.Close() })
	conn, err := grpc.NewClient(srv.Addr, grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		t.Fatal(err)
	}
	client, err := pubsub.NewClient(context.Background(), project, option.WithGRPCConn(conn))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = client.Close() })
	if createTopic {
		_, err = client.TopicAdminClient.CreateTopic(context.Background(), &pubsubpb.Topic{Name: "projects/" + project + "/topics/" + topic})
		if err != nil {
			t.Fatal(err)
		}
	}
	return srv, client
}

func TestPublishDeliversTheBodyUnchanged(t *testing.T) {
	srv, client := fake(t, true)
	p := NewWithClient(client, project, topic)
	body := []byte(`{"idempotency_key":"demo/main:12","job_name":"demo/main","build_number":12}`)

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := p.Publish(ctx, body); err != nil {
		t.Fatalf("publish: %v", err)
	}
	p.pub.Stop()
	msgs := srv.Messages()
	if len(msgs) != 1 || string(msgs[0].Data) != string(body) {
		t.Fatalf("got %d messages, want one with the same body: %+v", len(msgs), msgs)
	}
}

func TestPingSucceedsWhenTheTopicExistsAndFailsWhenItDoesNot(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	_, client := fake(t, true)
	if err := NewWithClient(client, project, topic).Ping(ctx); err != nil {
		t.Fatalf("ping with topic: %v", err)
	}
	_, empty := fake(t, false)
	if err := NewWithClient(empty, project, topic).Ping(ctx); err == nil {
		t.Fatal("ping must fail when the topic does not exist (the ingress must report not ready)")
	}
}

func TestPublishToAMissingTopicIsAnError(t *testing.T) {
	_, client := fake(t, false)
	p := NewWithClient(client, project, topic)
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := p.Publish(ctx, []byte("x")); err == nil {
		t.Fatal("publishing to a missing topic must fail, so the webhook is answered with 503 and Jenkins retries")
	}
}

func TestPublishHonoursCancellation(t *testing.T) {
	_, client := fake(t, true)
	p := NewWithClient(client, project, topic)
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if err := p.Publish(ctx, []byte("x")); err == nil {
		t.Fatal("a cancelled context must not report success")
	}
}

func TestNewRequiresProjectAndTopic(t *testing.T) {
	for _, c := range [][2]string{{"", topic}, {project, ""}, {"", ""}} {
		if _, err := New(context.Background(), c[0], c[1]); err == nil {
			t.Fatalf("New(%q,%q) must fail", c[0], c[1])
		}
	}
}
