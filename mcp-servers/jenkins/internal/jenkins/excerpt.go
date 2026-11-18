package jenkins

import (
	"fmt"
	"regexp"
	"strings"
)

const (
	stageStart = "[Pipeline] { ("
	stageEnd   = "[Pipeline] }"
	// maxLineLen keeps one pathological line from blowing the output budget.
	maxLineLen = 400
)

// errorLine matches lines that usually carry the failure signal.
var errorLine = regexp.MustCompile(
	`(?i)\b(error|errors|fail|failed|failure|exception|traceback|assert\w*|fatal|refused|denied|timed? ?out|not found)\b|^E\s`)

// StageLines returns the console lines of the named stage, or nil if the log
// has no such stage.
func StageLines(log []string, stage string) []string {
	var out []string
	in := false
	for _, l := range log {
		switch {
		case !in && l == stageStart+stage+")":
			in = true
		case in && l == stageEnd:
			return out
		case in:
			out = append(out, l)
		}
	}
	if in {
		return out // log ended inside the stage
	}
	return nil
}

// Excerpt is the reduced log returned to the agent.
type Excerpt struct {
	Stage         string   `json:"stage" jsonschema:"the stage the excerpt comes from"`
	Lines         []string `json:"lines" jsonschema:"error-bearing lines with one line of context"`
	Truncated     bool     `json:"truncated" jsonschema:"true if lines were omitted to respect max_lines"`
	TotalLogLines int      `json:"total_log_lines" jsonschema:"size of the full log, for reference only"`
}

// ExtractExcerpt keeps lines matching error patterns (plus one line of
// context before each), then fits them into maxLines by keeping the head and
// tail. Falls back to the last lines of the stage when nothing matches. When
// stage is empty or missing from the log, the whole log is searched.
func ExtractExcerpt(log []string, stage string, maxLines int) Excerpt {
	lines := log
	if s := StageLines(log, stage); stage != "" && s != nil {
		lines = s
	}
	keep := make([]bool, len(lines))
	matched := false
	for i, l := range lines {
		if errorLine.MatchString(l) {
			matched = true
			keep[i] = true
			if i > 0 {
				keep[i-1] = true
			}
		}
	}
	var picked []string
	for i, l := range lines {
		if keep[i] && strings.TrimSpace(l) != "" {
			picked = append(picked, clip(l))
		}
	}
	if !matched {
		start := max(0, len(lines)-maxLines)
		for _, l := range lines[start:] {
			picked = append(picked, clip(l))
		}
	}

	ex := Excerpt{Stage: stage, TotalLogLines: len(log)}
	if len(picked) > maxLines {
		ex.Truncated = true
		head := (maxLines - 1) / 2
		tail := maxLines - 1 - head
		omitted := len(picked) - head - tail
		ex.Lines = append(ex.Lines, picked[:head]...)
		ex.Lines = append(ex.Lines, fmt.Sprintf("... (%d lines omitted)", omitted))
		ex.Lines = append(ex.Lines, picked[len(picked)-tail:]...)
	} else {
		ex.Lines = picked
	}
	if ex.Lines == nil {
		ex.Lines = []string{}
	}
	return ex
}

func clip(l string) string {
	if len(l) > maxLineLen {
		return l[:maxLineLen] + "...(clipped)"
	}
	return l
}
