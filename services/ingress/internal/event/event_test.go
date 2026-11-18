package event

import (
	"errors"
	"strings"
	"testing"
)

func valid() Webhook {
	return Webhook{JobName: "demo/main", BuildNumber: 42, Status: "FAILURE",
		BuildURL: "https://jenkins.example/job/demo/42/", Repo: "acme/demo", CommitSHA: "abc1234"}
}

func TestValidate(t *testing.T) {
	t.Parallel()
	tests := []struct {
		name    string
		mutate  func(*Webhook)
		wantErr string // substring; "" means ok
		notFail bool
	}{
		{"valid", func(*Webhook) {}, "", false},
		{"minimal", func(w *Webhook) { w.BuildURL, w.Repo, w.CommitSHA = "", "", "" }, "", false},
		{"empty job", func(w *Webhook) { w.JobName = "" }, "job_name", false},
		{"job with shell chars", func(w *Webhook) { w.JobName = "a;rm -rf /" }, "job_name", false},
		{"job too long", func(w *Webhook) { w.JobName = strings.Repeat("a", 201) }, "job_name", false},
		{"zero build", func(w *Webhook) { w.BuildNumber = 0 }, "build_number", false},
		{"negative build", func(w *Webhook) { w.BuildNumber = -1 }, "build_number", false},
		{"unknown status", func(w *Webhook) { w.Status = "BOOM" }, "status", false},
		{"success ignored", func(w *Webhook) { w.Status = "SUCCESS" }, "", true},
		{"javascript url", func(w *Webhook) { w.BuildURL = "javascript:alert(1)" }, "build_url", false},
		{"relative url", func(w *Webhook) { w.BuildURL = "/job/1" }, "build_url", false},
		{"bad repo", func(w *Webhook) { w.Repo = "no-slash" }, "repo", false},
		{"bad sha", func(w *Webhook) { w.CommitSHA = "xyz" }, "commit_sha", false},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			t.Parallel()
			w := valid()
			tt.mutate(&w)
			err := w.Validate()
			switch {
			case tt.notFail:
				if !errors.Is(err, ErrNotFailure) {
					t.Fatalf("want ErrNotFailure, got %v", err)
				}
			case tt.wantErr == "":
				if err != nil {
					t.Fatalf("unexpected error: %v", err)
				}
			default:
				if err == nil || !strings.Contains(err.Error(), tt.wantErr) {
					t.Fatalf("want error containing %q, got %v", tt.wantErr, err)
				}
			}
		})
	}
}

func TestIdempotencyKey(t *testing.T) {
	t.Parallel()
	if got := valid().IdempotencyKey(); got != "demo/main:42" {
		t.Fatalf("got %q", got)
	}
}
