// Package server implements the ingress HTTP API.
package server

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"time"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/propagation"

	"github.com/Franklin34/pipeline-doctor-agent/services/ingress/internal/event"
	"github.com/Franklin34/pipeline-doctor-agent/services/ingress/internal/signature"
)

// Publisher sends an encoded event to the queue and reports readiness.
type Publisher interface {
	Publish(ctx context.Context, body []byte) error
	Ping(ctx context.Context) error
}

// Deduper claims idempotency keys.
type Deduper interface {
	Claim(ctx context.Context, key string) (bool, error)
	Release(ctx context.Context, key string) error
}

// Deps are the server's collaborators and settings.
type Deps struct {
	Secret       []byte
	MaxBodyBytes int64
	Publisher    Publisher
	Dedupe       Deduper
	Metrics      *Metrics
	Log          *slog.Logger
	Now          func() time.Time // injectable clock; defaults to time.Now
}

type server struct{ Deps }

// New returns the HTTP handler with all routes.
func New(d Deps) http.Handler {
	if d.Now == nil {
		d.Now = time.Now
	}
	if d.Log == nil {
		d.Log = slog.Default()
	}
	if d.Metrics == nil {
		d.Metrics = NewMetrics()
	}
	s := &server{d}
	mux := http.NewServeMux()
	mux.HandleFunc("POST /webhooks/jenkins", s.webhook)
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, _ *http.Request) { w.WriteHeader(http.StatusOK) })
	mux.HandleFunc("GET /readyz", s.ready)
	mux.Handle("GET /metrics", d.Metrics.Handler())
	return mux
}

func (s *server) ready(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := context.WithTimeout(r.Context(), 2*time.Second)
	defer cancel()
	if err := s.Publisher.Ping(ctx); err != nil {
		s.Log.Warn("not ready", "error", err)
		http.Error(w, "queue unavailable", http.StatusServiceUnavailable)
		return
	}
	w.WriteHeader(http.StatusOK)
}

func (s *server) webhook(w http.ResponseWriter, r *http.Request) {
	start := s.Now()
	code := s.handleWebhook(w, r)
	s.Metrics.observeRequest(code)
	s.Metrics.latency.Observe(s.Now().Sub(start).Seconds())
}

// handleWebhook returns the HTTP status it wrote.
func (s *server) handleWebhook(w http.ResponseWriter, r *http.Request) int {
	ctx := otel.GetTextMapPropagator().Extract(r.Context(), propagation.HeaderCarrier(r.Header))
	ctx, span := otel.Tracer("ingress").Start(ctx, "webhook.jenkins")
	defer span.End()

	// 1. Read the body with a hard cap, before anything else.
	body, err := io.ReadAll(http.MaxBytesReader(w, r.Body, s.MaxBodyBytes))
	if err != nil {
		var tooBig *http.MaxBytesError
		if errors.As(err, &tooBig) {
			return reply(w, http.StatusRequestEntityTooLarge, "payload too large")
		}
		return reply(w, http.StatusBadRequest, "cannot read body")
	}

	// 2. Authenticate over the raw bytes. Nothing is parsed before this passes.
	if err := signature.Verify(s.Secret, body, r.Header.Get(signature.Header)); err != nil {
		s.Log.Warn("rejected webhook", "reason", "bad signature", "remote", r.RemoteAddr)
		return reply(w, http.StatusUnauthorized, "invalid signature")
	}

	// 3. Parse and validate.
	var hook event.Webhook
	if err := json.Unmarshal(body, &hook); err != nil {
		return reply(w, http.StatusBadRequest, "malformed JSON")
	}
	if err := hook.Validate(); err != nil {
		if errors.Is(err, event.ErrNotFailure) {
			return replyJSON(w, http.StatusOK, map[string]string{"status": "ignored"})
		}
		return reply(w, http.StatusUnprocessableEntity, err.Error())
	}
	key := hook.IdempotencyKey()
	span.SetAttributes(attribute.String("pipeline.idempotency_key", key))
	log := s.Log.With("idempotency_key", key)

	// 4. Deduplicate. Fail closed: if we cannot tell, ask Jenkins to retry.
	first, err := s.Dedupe.Claim(ctx, key)
	if err != nil {
		span.SetStatus(codes.Error, "dedupe unavailable")
		log.Error("dedupe failed", "error", err)
		return reply(w, http.StatusServiceUnavailable, "temporarily unavailable")
	}
	if !first {
		s.Metrics.duplicates.Inc()
		log.Info("duplicate delivery dropped")
		return replyJSON(w, http.StatusAccepted, map[string]string{"status": "duplicate", "idempotency_key": key})
	}

	// 5. Publish, carrying the trace context in the event.
	carrier := propagation.MapCarrier{}
	otel.GetTextMapPropagator().Inject(ctx, carrier)
	payload, err := json.Marshal(hook.ToEvent(s.Now(), carrier.Get("traceparent")))
	if err == nil {
		err = s.Publisher.Publish(ctx, payload)
	}
	if err != nil {
		s.Metrics.publishErrors.Inc()
		span.SetStatus(codes.Error, "publish failed")
		log.Error("publish failed", "error", err)
		// Release so Jenkins' retry is not dropped as a duplicate.
		if rerr := s.Dedupe.Release(context.WithoutCancel(ctx), key); rerr != nil {
			log.Error("release failed; key will expire via TTL", "error", rerr)
		}
		return reply(w, http.StatusServiceUnavailable, "temporarily unavailable")
	}
	log.Info("pipeline failure accepted")
	return replyJSON(w, http.StatusAccepted, map[string]string{"status": "accepted", "idempotency_key": key})
}

func reply(w http.ResponseWriter, code int, msg string) int {
	return replyJSON(w, code, map[string]string{"error": msg})
}

func replyJSON(w http.ResponseWriter, code int, v any) int {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(v)
	return code
}
