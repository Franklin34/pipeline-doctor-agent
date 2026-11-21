package config

import (
	"testing"
	"time"
)

func env(m map[string]string) func(string) string { return func(k string) string { return m[k] } }

func TestLoad(t *testing.T) {
	t.Parallel()
	secret := "0123456789abcdef"
	tests := []struct {
		name    string
		env     map[string]string
		wantErr bool
	}{
		{"defaults", map[string]string{"WEBHOOK_SHARED_SECRET": secret}, false},
		{"missing secret", map[string]string{}, true},
		{"short secret", map[string]string{"WEBHOOK_SHARED_SECRET": "short"}, true},
		{"bad ttl", map[string]string{"WEBHOOK_SHARED_SECRET": secret, "IDEMPOTENCY_TTL": "soon"}, true},
		{"zero ttl", map[string]string{"WEBHOOK_SHARED_SECRET": secret, "IDEMPOTENCY_TTL": "0s"}, true},
		{"bad max body", map[string]string{"WEBHOOK_SHARED_SECRET": secret, "MAX_BODY_BYTES": "-1"}, true},
		{"pubsub with project", map[string]string{"WEBHOOK_SHARED_SECRET": secret, "QUEUE_BACKEND": "pubsub", "PUBSUB_PROJECT": "p"}, false},
		{"pubsub without project", map[string]string{"WEBHOOK_SHARED_SECRET": secret, "QUEUE_BACKEND": "pubsub"}, true},
		{"unknown backend", map[string]string{"WEBHOOK_SHARED_SECRET": secret, "QUEUE_BACKEND": "kafka"}, true},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			t.Parallel()
			_, err := Load(env(tt.env))
			if (err != nil) != tt.wantErr {
				t.Fatalf("err=%v wantErr=%v", err, tt.wantErr)
			}
		})
	}
}

func TestLoadDefaults(t *testing.T) {
	t.Parallel()
	c, err := Load(env(map[string]string{"WEBHOOK_SHARED_SECRET": "0123456789abcdef"}))
	if err != nil {
		t.Fatal(err)
	}
	if c.Addr != ":8080" || c.IdempotencyTTL != 24*time.Hour || c.Stream != "pipeline-failed" {
		t.Fatalf("unexpected defaults: %+v", c)
	}
}
