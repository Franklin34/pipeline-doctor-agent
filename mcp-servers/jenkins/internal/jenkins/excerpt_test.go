package jenkins

import (
	"fmt"
	"strings"
	"testing"
)

var sampleLog = []string{
	"Started by user dev1",
	"[Pipeline] { (Checkout)",
	"Cloning repository",
	"[Pipeline] }",
	"[Pipeline] { (Test)",
	"+ pytest -q",
	"collected 4 items",
	"E       assert 10.0 == 90.0",
	"FAILED tests/test_x.py::test_a",
	"[Pipeline] }",
	"Finished: FAILURE",
}

func TestStageLines(t *testing.T) {
	t.Parallel()
	got := StageLines(sampleLog, "Test")
	if len(got) != 4 || got[0] != "+ pytest -q" {
		t.Fatalf("unexpected lines: %v", got)
	}
	if StageLines(sampleLog, "Nope") != nil {
		t.Fatal("unknown stage must return nil")
	}
}

func TestExtractExcerptKeepsErrorsAndSkipsOtherStages(t *testing.T) {
	t.Parallel()
	ex := ExtractExcerpt(sampleLog, "Test", 50)
	joined := strings.Join(ex.Lines, "\n")
	if !strings.Contains(joined, "assert 10.0 == 90.0") || !strings.Contains(joined, "FAILED tests/test_x.py") {
		t.Fatalf("missing error lines: %v", ex.Lines)
	}
	if strings.Contains(joined, "Cloning") {
		t.Fatal("lines from other stages must not leak into the excerpt")
	}
	if ex.TotalLogLines != len(sampleLog) || ex.Truncated {
		t.Fatalf("unexpected metadata: %+v", ex)
	}
}

func TestExtractExcerptTruncatesHeadAndTail(t *testing.T) {
	t.Parallel()
	var log []string
	log = append(log, "[Pipeline] { (Test)")
	for i := 0; i < 100; i++ {
		log = append(log, fmt.Sprintf("ERROR number %d", i))
	}
	log = append(log, "[Pipeline] }")
	ex := ExtractExcerpt(log, "Test", 10)
	if len(ex.Lines) != 10 || !ex.Truncated {
		t.Fatalf("want 10 lines truncated, got %d truncated=%v", len(ex.Lines), ex.Truncated)
	}
	if ex.Lines[0] != "ERROR number 0" || ex.Lines[9] != "ERROR number 99" ||
		!strings.Contains(ex.Lines[4], "omitted") {
		t.Fatalf("expected head, omitted marker and tail: %v", ex.Lines)
	}
}

func TestExtractExcerptFallsBackToTailWhenNoErrorLines(t *testing.T) {
	t.Parallel()
	log := []string{"[Pipeline] { (Build)", "step 1", "step 2", "step 3", "[Pipeline] }"}
	ex := ExtractExcerpt(log, "Build", 2)
	if len(ex.Lines) != 2 || ex.Lines[1] != "step 3" {
		t.Fatalf("got %v", ex.Lines)
	}
}

func TestExtractExcerptClipsLongLines(t *testing.T) {
	t.Parallel()
	log := []string{"[Pipeline] { (S)", "ERROR " + strings.Repeat("x", 5000), "[Pipeline] }"}
	ex := ExtractExcerpt(log, "S", 5)
	if len(ex.Lines[0]) > maxLineLen+20 {
		t.Fatalf("line not clipped: %d bytes", len(ex.Lines[0]))
	}
}
