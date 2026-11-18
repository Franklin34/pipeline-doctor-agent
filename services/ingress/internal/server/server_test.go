package server

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/Franklin34/pipeline-doctor-agent/services/ingress/internal/event"
	"github.com/Franklin34/pipeline-doctor-agent/services/ingress/internal/signature"
	"github.com/Franklin34/pipeline-doctor-agent/services/ingress/internal/telemetry"
)

var secret = []byte("0123456789abcdef")

type fakeQueue struct {
	mu        sync.Mutex
	published [][]byte
	err       error
	pingErr   error
}

func (f *fakeQueue) Publish(_ context.Context, b []byte) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.err != nil {
		return f.err
	}
	f.published = append(f.published, b)
	return nil
}
func (f *fakeQueue) Ping(context.Context) error { return f.pingErr }
func (f *fakeQueue) count() int                 { f.mu.Lock(); defer f.mu.Unlock(); return len(f.published) }

type fakeDedupe struct {
	mu   sync.Mutex
	seen map[string]bool
	err  error
}

func (f *fakeDedupe) Claim(_ context.Context, k string) (bool, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.err != nil {
		return false, f.err
	}
	if f.seen[k] {
		return false, nil
	}
	f.seen[k] = true
	return true, nil
}
func (f *fakeDedupe) Release(_ context.Context, k string) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	delete(f.seen, k)
	return nil
}

type harness struct {
	h http.Handler
	q *fakeQueue
	d *fakeDedupe
}

func newHarness() *harness {
	q, d := &fakeQueue{}, &fakeDedupe{seen: map[string]bool{}}
	h := New(Deps{
		Secret: secret, MaxBodyBytes: 1024, Publisher: q, Dedupe: d,
		Metrics: NewMetrics(), Log: slog.New(slog.NewTextHandler(io.Discard, nil)),
		Now: func() time.Time { return time.Date(2026, 1, 2, 3, 4, 5, 0, time.UTC) },
	})
	return &harness{h, q, d}
}

const goodBody = `{"job_name":"demo/main","build_number":7,"status":"FAILURE","repo":"acme/demo","commit_sha":"abc1234"}`

func (hn *harness) post(body string, sig string) *httptest.ResponseRecorder {
	req := httptest.NewRequest(http.MethodPost, "/webhooks/jenkins", strings.NewReader(body))
	if sig != "" {
		req.Header.Set(signature.Header, sig)
	}
	rec := httptest.NewRecorder()
	hn.h.ServeHTTP(rec, req)
	return rec
}

func signed(body string) string { return signature.Sign(secret, []byte(body)) }

func TestWebhook(t *testing.T) {
	tests := []struct {
		name          string
		body, sig     string
		queueErr      error
		dedupeErr     error
		wantCode      int
		wantPublished int
	}{
		{"accepted", goodBody, signed(goodBody), nil, nil, 202, 1},
		{"missing signature", goodBody, "", nil, nil, 401, 0},
		{"invalid signature", goodBody, "sha256=deadbeef", nil, nil, 401, 0},
		{"signature for other body", goodBody, signed(`{}`), nil, nil, 401, 0},
		{"malformed json", "{nope", signed("{nope"), nil, nil, 400, 0},
		{"invalid job name", `{"job_name":"","build_number":1,"status":"FAILURE"}`,
			signed(`{"job_name":"","build_number":1,"status":"FAILURE"}`), nil, nil, 422, 0},
		{"success build ignored", `{"job_name":"x","build_number":1,"status":"SUCCESS"}`,
			signed(`{"job_name":"x","build_number":1,"status":"SUCCESS"}`), nil, nil, 200, 0},
		{"payload too large", strings.Repeat("a", 2048), signed(strings.Repeat("a", 2048)), nil, nil, 413, 0},
		{"queue failure", goodBody, signed(goodBody), errors.New("redis down"), nil, 503, 0},
		{"dedupe failure fails closed", goodBody, signed(goodBody), nil, errors.New("redis down"), 503, 0},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			hn := newHarness()
			hn.q.err, hn.d.err = tt.queueErr, tt.dedupeErr
			rec := hn.post(tt.body, tt.sig)
			if rec.Code != tt.wantCode {
				t.Fatalf("code=%d want %d body=%s", rec.Code, tt.wantCode, rec.Body)
			}
			if got := hn.q.count(); got != tt.wantPublished {
				t.Fatalf("published=%d want %d", got, tt.wantPublished)
			}
		})
	}
}

func TestPublishedEventContents(t *testing.T) {
	hn := newHarness()
	if rec := hn.post(goodBody, signed(goodBody)); rec.Code != 202 {
		t.Fatalf("code=%d", rec.Code)
	}
	var ev event.PipelineFailed
	if err := json.Unmarshal(hn.q.published[0], &ev); err != nil {
		t.Fatal(err)
	}
	if ev.IdempotencyKey != "demo/main:7" || ev.JobName != "demo/main" || ev.BuildNumber != 7 ||
		ev.Repo != "acme/demo" || ev.ReceivedAt.Year() != 2026 {
		t.Fatalf("unexpected event: %+v", ev)
	}
}

func TestDuplicateDelivery(t *testing.T) {
	hn := newHarness()
	first := hn.post(goodBody, signed(goodBody))
	second := hn.post(goodBody, signed(goodBody))
	if first.Code != 202 || second.Code != 202 {
		t.Fatalf("codes %d %d", first.Code, second.Code)
	}
	if !strings.Contains(second.Body.String(), `"duplicate"`) {
		t.Fatalf("second response should say duplicate: %s", second.Body)
	}
	if hn.q.count() != 1 {
		t.Fatalf("published %d times, want 1", hn.q.count())
	}
}

// If publishing fails the key must be released, otherwise Jenkins' retry would
// be silently dropped as a "duplicate" and the failure never investigated.
func TestRetryAfterQueueFailureIsNotDropped(t *testing.T) {
	hn := newHarness()
	hn.q.err = errors.New("redis down")
	if rec := hn.post(goodBody, signed(goodBody)); rec.Code != 503 {
		t.Fatalf("code=%d", rec.Code)
	}
	hn.q.err = nil
	if rec := hn.post(goodBody, signed(goodBody)); rec.Code != 202 || hn.q.count() != 1 {
		t.Fatalf("retry code=%d published=%d", rec.Code, hn.q.count())
	}
}

func TestTraceContextPropagatedInEvent(t *testing.T) {
	shutdown, err := telemetry.Init(context.Background(), "", "test")
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = shutdown(context.Background()) }()

	hn := newHarness()
	hn.post(goodBody, signed(goodBody))
	var ev event.PipelineFailed
	_ = json.Unmarshal(hn.q.published[0], &ev)
	if parts := strings.Split(ev.TraceParent, "-"); len(parts) != 4 || parts[0] != "00" {
		t.Fatalf("expected W3C traceparent, got %q", ev.TraceParent)
	}
}

func TestProbesAndMetrics(t *testing.T) {
	hn := newHarness()
	get := func(path string) *httptest.ResponseRecorder {
		rec := httptest.NewRecorder()
		hn.h.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, path, nil))
		return rec
	}
	if get("/healthz").Code != 200 || get("/readyz").Code != 200 {
		t.Fatal("healthz/readyz should be 200")
	}
	hn.q.pingErr = errors.New("down")
	if get("/readyz").Code != 503 {
		t.Fatal("readyz should be 503 when the queue is down")
	}

	hn.post(goodBody, signed(goodBody))
	hn.post(goodBody, signed(goodBody)) // duplicate
	hn.post(goodBody, "")               // 401
	hn.q.err = errors.New("x")
	hn.post(`{"job_name":"other","build_number":1,"status":"FAILURE"}`,
		signed(`{"job_name":"other","build_number":1,"status":"FAILURE"}`)) // publish error

	out := get("/metrics").Body.String()
	for _, want := range []string{
		`ingress_webhook_requests_total{code="202"} 2`,
		`ingress_webhook_requests_total{code="401"} 1`,
		`ingress_webhook_requests_total{code="503"} 1`,
		`ingress_webhook_duplicates_total 1`,
		`ingress_publish_errors_total 1`,
		`ingress_webhook_duration_seconds_count 4`,
	} {
		if !bytes.Contains([]byte(out), []byte(want)) {
			t.Errorf("metrics missing %q", want)
		}
	}
}

func TestOnlyPostAllowed(t *testing.T) {
	hn := newHarness()
	rec := httptest.NewRecorder()
	hn.h.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/webhooks/jenkins", nil))
	if rec.Code != http.StatusMethodNotAllowed {
		t.Fatalf("code=%d", rec.Code)
	}
}
