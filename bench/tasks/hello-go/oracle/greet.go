package main

// Greet returns a greeting for name; an empty name greets a stranger.
func Greet(name string) string {
	if name == "" {
		return "hello, stranger"
	}
	return "hello, " + name
}
