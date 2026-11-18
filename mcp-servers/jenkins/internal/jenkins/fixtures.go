package jenkins

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
)

type fixtureJob struct {
	Job         string                `json:"job"`
	Builds      []Build               `json:"builds"`
	Logs        map[string][]string   `json:"logs"`
	TestReports map[string]TestReport `json:"test_reports"`
}

// Fixtures is a Backend backed by fixtures/<scenario>/jenkins.json files.
type Fixtures struct {
	jobs map[string]*fixtureJob
}

var _ Backend = (*Fixtures)(nil)

// LoadFixtures reads every <dir>/*/jenkins.json. Job names must be unique.
func LoadFixtures(dir string) (*Fixtures, error) {
	paths, err := filepath.Glob(filepath.Join(dir, "*", "jenkins.json"))
	if err != nil {
		return nil, err
	}
	if len(paths) == 0 {
		return nil, fmt.Errorf("no jenkins.json fixtures found under %q", dir)
	}
	f := &Fixtures{jobs: map[string]*fixtureJob{}}
	for _, p := range paths {
		raw, err := os.ReadFile(p)
		if err != nil {
			return nil, err
		}
		var j fixtureJob
		if err := json.Unmarshal(raw, &j); err != nil {
			return nil, fmt.Errorf("%s: %w", p, err)
		}
		if _, dup := f.jobs[j.Job]; dup || j.Job == "" {
			return nil, fmt.Errorf("%s: empty or duplicate job name %q", p, j.Job)
		}
		for i := range j.Builds {
			j.Builds[i].Job = j.Job
		}
		f.jobs[j.Job] = &j
	}
	return f, nil
}

func (f *Fixtures) job(name string) (*fixtureJob, error) {
	j, ok := f.jobs[name]
	if !ok {
		return nil, fmt.Errorf("job %q: %w", name, ErrNotFound)
	}
	return j, nil
}

// GetBuild implements Backend.
func (f *Fixtures) GetBuild(_ context.Context, job string, build int) (Build, error) {
	j, err := f.job(job)
	if err != nil {
		return Build{}, err
	}
	for _, b := range j.Builds {
		if b.Number == build {
			return b, nil
		}
	}
	return Build{}, fmt.Errorf("build %d of job %q: %w", build, job, ErrNotFound)
}

// GetLog implements Backend.
func (f *Fixtures) GetLog(ctx context.Context, job string, build int) ([]string, error) {
	if _, err := f.GetBuild(ctx, job, build); err != nil {
		return nil, err
	}
	log, ok := f.jobs[job].Logs[strconv.Itoa(build)]
	if !ok {
		return nil, fmt.Errorf("console log of build %d of job %q: %w", build, job, ErrNotFound)
	}
	return log, nil
}

// GetTestReport implements Backend.
func (f *Fixtures) GetTestReport(ctx context.Context, job string, build int) (TestReport, error) {
	if _, err := f.GetBuild(ctx, job, build); err != nil {
		return TestReport{}, err
	}
	r, ok := f.jobs[job].TestReports[strconv.Itoa(build)]
	if !ok {
		return TestReport{}, fmt.Errorf("test report of build %d of job %q: %w", build, job, ErrNotFound)
	}
	if r.Failed == nil {
		r.Failed = []FailedTest{}
	}
	return r, nil
}

// LastSuccessful implements Backend.
func (f *Fixtures) LastSuccessful(_ context.Context, job string) (Build, error) {
	j, err := f.job(job)
	if err != nil {
		return Build{}, err
	}
	var best *Build
	for i := range j.Builds {
		if j.Builds[i].Result == "SUCCESS" && (best == nil || j.Builds[i].Number > best.Number) {
			best = &j.Builds[i]
		}
	}
	if best == nil {
		return Build{}, fmt.Errorf("successful build of job %q: %w", job, ErrNotFound)
	}
	return *best, nil
}

// Rebuild implements Backend. Mock mode only pretends.
func (f *Fixtures) Rebuild(ctx context.Context, job string, build int) (string, error) {
	if _, err := f.GetBuild(ctx, job, build); err != nil {
		return "", err
	}
	return "queued (mock mode: nothing was actually rebuilt)", nil
}
