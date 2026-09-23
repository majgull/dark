#!/bin/bash
# hidden acceptance for hello-go; runs at the repo root in the staging VM
export HOME="${HOME:-/root}" GOCACHE="${GOCACHE:-/root/.cache/go-build}" GOPATH="${GOPATH:-/root/go}"
export PATH="$PATH:/usr/local/go/bin"
fail=0
if [ -f greet.go ]; then echo "CHECK greet-file ok"; else echo "CHECK greet-file fail"; fail=1; fi
if [ -f greet_test.go ]; then echo "CHECK test-file ok"; else echo "CHECK test-file fail"; fail=1; fi
cat > zz_acceptance_test.go <<'EOF'
package main

import "testing"

func TestAcceptGreet(t *testing.T) {
	if got := Greet("dark"); got != "hello, dark" {
		t.Fatalf("Greet(dark) = %q", got)
	}
	if got := Greet(""); got != "hello, stranger" {
		t.Fatalf("Greet(\"\") = %q", got)
	}
}
EOF
if go test ./... >/tmp/acc.log 2>&1; then echo "CHECK greet ok"; else echo "CHECK greet fail"; tail -20 /tmp/acc.log; fail=1; fi
rm -f zz_acceptance_test.go
exit $fail
