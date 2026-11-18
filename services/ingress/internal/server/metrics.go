package server

import (
	"net/http"
	"strconv"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promhttp"
)

// Metrics are the ingress Prometheus collectors.
type Metrics struct {
	reg           *prometheus.Registry
	requests      *prometheus.CounterVec
	duplicates    prometheus.Counter
	publishErrors prometheus.Counter
	latency       prometheus.Histogram
}

// NewMetrics registers collectors on a private registry (keeps tests isolated).
func NewMetrics() *Metrics {
	m := &Metrics{
		reg: prometheus.NewRegistry(),
		requests: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "ingress_webhook_requests_total", Help: "Webhook requests by HTTP status code.",
		}, []string{"code"}),
		duplicates: prometheus.NewCounter(prometheus.CounterOpts{
			Name: "ingress_webhook_duplicates_total", Help: "Duplicate deliveries dropped.",
		}),
		publishErrors: prometheus.NewCounter(prometheus.CounterOpts{
			Name: "ingress_publish_errors_total", Help: "Failures publishing to the queue.",
		}),
		latency: prometheus.NewHistogram(prometheus.HistogramOpts{
			Name: "ingress_webhook_duration_seconds", Help: "Webhook handling latency.",
			Buckets: []float64{.001, .0025, .005, .01, .025, .05, .1, .25, .5, 1},
		}),
	}
	m.reg.MustRegister(m.requests, m.duplicates, m.publishErrors, m.latency)
	return m
}

// Handler serves /metrics.
func (m *Metrics) Handler() http.Handler {
	return promhttp.HandlerFor(m.reg, promhttp.HandlerOpts{})
}

func (m *Metrics) observeRequest(code int) { m.requests.WithLabelValues(strconv.Itoa(code)).Inc() }
