// Package event defines the webhook payload and the PipelineFailed event
// published to the queue.
package event

import (
	"errors"
	"fmt"
	"net/url"
	"regexp"
	"strconv"
	"time"
)

// Webhook is the payload Jenkins posts to /webhooks/jenkins.
type Webhook struct {
	JobName     string `json:"job_name"`
	BuildNumber int    `json:"build_number"`
	Status      string `json:"status"`
	BuildURL    string `json:"build_url,omitempty"`
	Repo        string `json:"repo,omitempty"`
	CommitSHA   string `json:"commit_sha,omitempty"`
}

// PipelineFailed is the event consumed by the orchestrator. The schema is a
// contract with the Python worker: add fields, never repurpose them.
type PipelineFailed struct {
	IdempotencyKey string    `json:"idempotency_key"`
	JobName        string    `json:"job_name"`
	BuildNumber    int       `json:"build_number"`
	BuildURL       string    `json:"build_url,omitempty"`
	Repo           string    `json:"repo,omitempty"`
	CommitSHA      string    `json:"commit_sha,omitempty"`
	ReceivedAt     time.Time `json:"received_at"`
	// TraceParent is the W3C trace context so the worker continues the trace.
	TraceParent string `json:"traceparent,omitempty"`
}

var (
	jobNameRe = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9 ._/-]{0,199}$`)
	repoRe    = regexp.MustCompile(`^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$`)
	shaRe     = regexp.MustCompile(`^[0-9a-fA-F]{7,40}$`)
)

// ErrNotFailure marks a valid payload for a build that did not fail.
var ErrNotFailure = errors.New("build status is not FAILURE")

var knownStatuses = map[string]bool{
	"SUCCESS": true, "FAILURE": true, "UNSTABLE": true, "ABORTED": true, "NOT_BUILT": true,
}

// Validate checks the payload strictly. It returns ErrNotFailure for valid
// payloads that should simply be ignored.
func (w Webhook) Validate() error {
	if !jobNameRe.MatchString(w.JobName) {
		return errors.New("job_name is missing or has invalid characters")
	}
	if w.BuildNumber < 1 {
		return errors.New("build_number must be a positive integer")
	}
	if !knownStatuses[w.Status] {
		return fmt.Errorf("status %q is not a known Jenkins result", w.Status)
	}
	if w.BuildURL != "" {
		u, err := url.Parse(w.BuildURL)
		if err != nil || (u.Scheme != "http" && u.Scheme != "https") || u.Host == "" {
			return errors.New("build_url must be an absolute http(s) URL")
		}
	}
	if w.Repo != "" && !repoRe.MatchString(w.Repo) {
		return errors.New("repo must look like owner/name")
	}
	if w.CommitSHA != "" && !shaRe.MatchString(w.CommitSHA) {
		return errors.New("commit_sha must be 7-40 hex characters")
	}
	if w.Status != "FAILURE" {
		return ErrNotFailure
	}
	return nil
}

// IdempotencyKey is job_name:build_number.
func (w Webhook) IdempotencyKey() string {
	return w.JobName + ":" + strconv.Itoa(w.BuildNumber)
}

// ToEvent converts a validated webhook into a queue event.
func (w Webhook) ToEvent(now time.Time, traceparent string) PipelineFailed {
	return PipelineFailed{
		IdempotencyKey: w.IdempotencyKey(),
		JobName:        w.JobName,
		BuildNumber:    w.BuildNumber,
		BuildURL:       w.BuildURL,
		Repo:           w.Repo,
		CommitSHA:      w.CommitSHA,
		ReceivedAt:     now.UTC(),
		TraceParent:    traceparent,
	}
}
