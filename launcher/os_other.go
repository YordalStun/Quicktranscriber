//go:build !windows

package main

import (
	"fmt"
	"os"
	"os/exec"
)

func hideWindow(cmd *exec.Cmd) {}

func setNewConsole(cmd *exec.Cmd) {
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr
	cmd.Stdin = os.Stdin
}

func showError(msg string) {
	fmt.Fprintln(os.Stderr, appName+": "+msg)
}
