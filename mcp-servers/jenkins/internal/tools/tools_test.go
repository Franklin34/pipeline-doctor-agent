package tools

import (
	"context"
	"encoding/json"
	"strings"
	"testing"

	"github.com/modelcontextprotocol/go-sdk/mcp"

	"github.com/Franklin34/pipeline-doctor-agent/mcp-servers/jenkins/internal/jenkins"
)

// connect starts the server in-process and returns a client session.
func connect(t *testing.T, opts Options) *mcp.ClientSession {
	t.Helper()
	backend, err := jenkins.LoadFixtures("../../../../fixtures")
	if err != nil {
		t.Fatal(err)
	}
	ctx := context.Background()
	st, ct := mcp.NewInMemoryTransports()
	ss, err := New(backend, opts).Connect(ctx, st, nil)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = ss.Close() })
	cs, err := mcp.NewClient(&mcp.Implementation{Name: "test", Version: "0"}, nil).Connect(ctx, ct, nil)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = cs.Close() })
	return cs
}

func call(t *testing.T, cs *mcp.ClientSession, tool string, args map[string]any) (text string, isErr bool) {
	t.Helper()
	res, err := cs.CallTool(context.Background(), &mcp.CallToolParams{Name: tool, Arguments: args})
	if err != nil {
		// Schema-level rejection surfaces as a protocol error; treat it as an error result.
		return err.Error(), true
	}
	var sb strings.Builder
	for _, c := range res.Content {
		if tc, ok := c.(*mcp.TextContent); ok {
			sb.WriteString(tc.Text)
		}
	}
	return sb.String(), res.IsError
}

func TestListToolsRebuildGatedByPolicy(t *testing.T) {
	names := func(cs *mcp.ClientSession) map[string]bool {
		res, err := cs.ListTools(context.Background(), nil)
		if err != nil {
			t.Fatal(err)
		}
		m := map[string]bool{}
		for _, tl := range res.Tools {
			m[tl.Name] = true
		}
		return m
	}
	off := names(connect(t, Options{}))
	for _, want := range []string{"get_build", "get_failed_log_excerpt", "get_test_report", "get_last_successful_build"} {
		if !off[want] {
			t.Errorf("missing tool %s", want)
		}
	}
	if off["rebuild"] {
		t.Fatal("rebuild must not exist unless enabled")
	}
	if !names(connect(t, Options{RebuildEnabled: true}))["rebuild"] {
		t.Fatal("rebuild must exist when enabled")
	}
}

func TestTools(t *testing.T) {
	cs := connect(t, Options{RebuildEnabled: true})
	tests := []struct {
		name     string
		tool     string
		args     map[string]any
		wantErr  bool
		contains []string
		absent   []string
	}{
		{"get_build ok", "get_build", map[string]any{"job": "demo-service/main", "build": 12}, false,
			[]string{`"result":"FAILURE"`, `"failed_stage":"Test"`, "c3d4e5f60718293a4b5c6d7e8f90123456789012"}, nil},
		{"get_build unknown job", "get_build", map[string]any{"job": "nope/main", "build": 1}, true,
			[]string{"not found", "Check the job name"}, nil},
		{"get_build unknown build", "get_build", map[string]any{"job": "demo-service/main", "build": 99}, true,
			[]string{"not found"}, nil},
		{"get_build bad job chars", "get_build", map[string]any{"job": "x;rm -rf /", "build": 1}, true,
			[]string{"invalid job"}, nil},
		{"get_build zero build", "get_build", map[string]any{"job": "demo-service/main", "build": 0}, true,
			[]string{"invalid build"}, nil},
		{"get_build wrong type", "get_build", map[string]any{"job": "demo-service/main", "build": "twelve"}, true, nil, nil},
		{"excerpt shows failure only", "get_failed_log_excerpt", map[string]any{"job": "demo-service/main", "build": 12}, false,
			[]string{"assert 10.0 == 90.0", "test_apply_discount", `"stage":"Test"`},
			[]string{"Cloning repository", "Successfully installed"}},
		{"excerpt bad max_lines", "get_failed_log_excerpt", map[string]any{"job": "demo-service/main", "build": 12, "max_lines": 5000}, true,
			[]string{"invalid max_lines"}, nil},
		{"excerpt on green build", "get_failed_log_excerpt", map[string]any{"job": "demo-service/main", "build": 11}, true,
			[]string{"succeeded"}, nil},
		{"test report", "get_test_report", map[string]any{"job": "demo-service/main", "build": 12}, false,
			[]string{`"passed":3`, "test_apply_discount"}, nil},
		{"last successful", "get_last_successful_build", map[string]any{"job": "demo-service/main"}, false,
			[]string{`"number":11`, "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"}, nil},
		{"last successful bad job", "get_last_successful_build", map[string]any{"job": ""}, true,
			[]string{"invalid job"}, nil},
		{"rebuild is mock only", "rebuild", map[string]any{"job": "demo-service/main", "build": 12}, false,
			[]string{"mock mode"}, nil},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			text, isErr := call(t, cs, tt.tool, tt.args)
			if isErr != tt.wantErr {
				t.Fatalf("isError=%v want %v; text=%s", isErr, tt.wantErr, text)
			}
			for _, s := range tt.contains {
				if !strings.Contains(text, s) {
					t.Errorf("output missing %q:\n%s", s, text)
				}
			}
			for _, s := range tt.absent {
				if strings.Contains(text, s) {
					t.Errorf("output must not contain %q:\n%s", s, text)
				}
			}
		})
	}
}

func TestExcerptIsMuchSmallerThanLog(t *testing.T) {
	cs := connect(t, Options{})
	text, isErr := call(t, cs, "get_failed_log_excerpt", map[string]any{"job": "demo-service/main", "build": 12, "max_lines": 3})
	if isErr {
		t.Fatal(text)
	}
	var ex jenkins.Excerpt
	if err := json.Unmarshal([]byte(text), &ex); err != nil {
		t.Fatal(err)
	}
	if len(ex.Lines) > 3 || ex.TotalLogLines <= len(ex.Lines) || !ex.Truncated {
		t.Fatalf("excerpt not bounded: %+v", ex)
	}
}
