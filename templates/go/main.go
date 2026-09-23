// Starting tree for a dark Go task. The task's spec says what to add; this
// file exists so the module builds and `go test ./...` is green from the
// first commit. Tasks that must change it say so on a `may edit:` line.
package main

import (
	"fmt"
	"os"
)

func main() {
	if len(os.Args) > 1 {
		fmt.Fprintf(os.Stderr, "app: unknown verb %q\n", os.Args[1])
		os.Exit(2)
	}
	fmt.Println("app: ok")
}
