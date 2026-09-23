package main

import "testing"

func TestGreet(t *testing.T) {
	if got := Greet("dark"); got != "hello, dark" {
		t.Fatalf("Greet(dark) = %q", got)
	}
	if got := Greet(""); got != "hello, stranger" {
		t.Fatalf("Greet(\"\") = %q", got)
	}
}
