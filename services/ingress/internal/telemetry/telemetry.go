// Package telemetry sets up OpenTelemetry tracing.
package telemetry

import (
	"context"
	"fmt"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracegrpc"
	"go.opentelemetry.io/otel/propagation"
	"go.opentelemetry.io/otel/sdk/resource"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	semconv "go.opentelemetry.io/otel/semconv/v1.26.0"
)

// Init installs the global tracer provider and W3C propagator. With an empty
// endpoint tracing stays a no-op (spans still carry valid IDs for propagation
// only if a provider is installed, so we always install the SDK provider).
// The returned function flushes and shuts the provider down.
func Init(ctx context.Context, endpoint, service string) (func(context.Context) error, error) {
	otel.SetTextMapPropagator(propagation.TraceContext{})

	opts := []sdktrace.TracerProviderOption{
		sdktrace.WithResource(resource.NewSchemaless(semconv.ServiceName(service))),
	}
	if endpoint != "" {
		exp, err := otlptracegrpc.New(ctx, otlptracegrpc.WithEndpointURL(endpoint))
		if err != nil {
			return nil, fmt.Errorf("telemetry: otlp exporter: %w", err)
		}
		opts = append(opts, sdktrace.WithBatcher(exp))
	}
	tp := sdktrace.NewTracerProvider(opts...)
	otel.SetTracerProvider(tp)
	return tp.Shutdown, nil
}
