// Package jenkins defines the backend contract the MCP tools depend on, plus a
// fixture-backed implementation for mock mode. A real HTTP backend has not been
// built; it would sit behind the same interface.
package jenkins

import (
	"context"
	"errors"
)

// ErrNotFound is returned when a job or build does not exist.
var ErrNotFound = errors.New("not found")

// Build is the compact build summary returned to the agent.
type Build struct {
	Job         string  `json:"job" jsonschema:"job name"`
	Number      int     `json:"number" jsonschema:"build number"`
	Result      string  `json:"result" jsonschema:"SUCCESS, FAILURE, UNSTABLE or ABORTED"`
	CommitSHA   string  `json:"commit_sha" jsonschema:"commit that was built"`
	FailedStage *string `json:"failed_stage" jsonschema:"name of the stage that failed, null if none"`
	DurationS   int     `json:"duration_s" jsonschema:"duration in seconds"`
	URL         string  `json:"url" jsonschema:"link to the build"`
}

// FailedTest is one failing test case.
type FailedTest struct {
	ClassName string `json:"class_name"`
	Name      string `json:"name"`
	Message   string `json:"message"`
}

// TestReport summarises JUnit results.
type TestReport struct {
	Passed  int          `json:"passed"`
	Skipped int          `json:"skipped"`
	Failed  []FailedTest `json:"failed"`
}

// Backend is everything the tools need from Jenkins. Implementations must be
// read-only, except Rebuild, which is only exposed when explicitly enabled.
type Backend interface {
	GetBuild(ctx context.Context, job string, build int) (Build, error)
	// GetLog returns the full console log. It is never returned to the agent
	// as-is; tools reduce it with ExtractExcerpt.
	GetLog(ctx context.Context, job string, build int) ([]string, error)
	GetTestReport(ctx context.Context, job string, build int) (TestReport, error)
	LastSuccessful(ctx context.Context, job string) (Build, error)
	Rebuild(ctx context.Context, job string, build int) (string, error)
}
