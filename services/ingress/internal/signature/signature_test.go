package signature

import "testing"

func TestVerify(t *testing.T) {
	t.Parallel()
	secret := []byte("s3cret")
	body := []byte(`{"job_name":"x"}`)
	good := Sign(secret, body)

	tests := []struct {
		name    string
		secret  []byte
		body    []byte
		header  string
		wantErr bool
	}{
		{"valid", secret, body, good, false},
		{"wrong secret", []byte("other"), body, good, true},
		{"tampered body", secret, []byte(`{"job_name":"y"}`), good, true},
		{"empty header", secret, body, "", true},
		{"no prefix", secret, body, good[len("sha256="):], true},
		{"bad hex", secret, body, "sha256=zzzz", true},
		{"truncated", secret, body, good[:len(good)-2], true},
		{"empty secret", nil, body, good, true},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			t.Parallel()
			err := Verify(tt.secret, tt.body, tt.header)
			if (err != nil) != tt.wantErr {
				t.Fatalf("err=%v wantErr=%v", err, tt.wantErr)
			}
		})
	}
}
