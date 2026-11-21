// Command jenkins-mcp is the read-only Jenkins MCP server (stdio transport).
//
// stdout carries the MCP protocol, so all logging goes to stderr.
package main

import (
	"context"
	"errors"
	"log/slog"
	"os"
	"os/signal"
	"syscall"

	"github.com/modelcontextprotocol/go-sdk/mcp"

	"github.com/Franklin34/pipeline-doctor-agent/mcp-servers/jenkins/internal/jenkins"
	"github.com/Franklin34/pipeline-doctor-agent/mcp-servers/jenkins/internal/tools"
)

func main() {
	log := slog.New(slog.NewJSONHandler(os.Stderr, nil))
	if err := run(log); err != nil {
		log.Error("fatal", "error", err)
		os.Exit(1)
	}
}

func run(log *slog.Logger) error {
	mode := os.Getenv("MCP_MODE")
	if mode == "" {
		mode = "mock"
	}
	if mode != "mock" {
		return errors.New("MCP_MODE=" + mode + " is not supported: only fixture mode exists (no real Jenkins backend has been built)")
	}
	dir := os.Getenv("FIXTURES_DIR")
	if dir == "" {
		dir = "fixtures"
	}
	backend, err := jenkins.LoadFixtures(dir)
	if err != nil {
		return err
	}
	opts := tools.Options{RebuildEnabled: os.Getenv("JENKINS_REBUILD_ENABLED") == "true"}
	log.Info("starting jenkins mcp server", "mode", mode, "fixtures", dir, "rebuild_enabled", opts.RebuildEnabled)

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
	defer stop()
	return tools.New(backend, opts).Run(ctx, &mcp.StdioTransport{})
}
