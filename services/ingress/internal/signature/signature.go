// Package signature verifies HMAC-SHA256 webhook signatures.
package signature

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"strings"
)

const prefix = "sha256="

// Header is the HTTP header carrying the signature.
const Header = "X-Signature-256"

// Sign returns the header value for body: "sha256=<hex>".
func Sign(secret, body []byte) string {
	mac := hmac.New(sha256.New, secret)
	mac.Write(body)
	return prefix + hex.EncodeToString(mac.Sum(nil))
}

// Verify checks header against body in constant time. The error is generic on
// purpose: callers must not tell attackers which part was wrong.
func Verify(secret, body []byte, header string) error {
	if len(secret) == 0 {
		return errors.New("signature: empty secret")
	}
	if !strings.HasPrefix(header, prefix) {
		return errors.New("signature: missing or malformed header")
	}
	got, err := hex.DecodeString(strings.TrimPrefix(header, prefix))
	if err != nil {
		return errors.New("signature: missing or malformed header")
	}
	mac := hmac.New(sha256.New, secret)
	mac.Write(body)
	if !hmac.Equal(got, mac.Sum(nil)) { // constant-time comparison
		return errors.New("signature: mismatch")
	}
	return nil
}
