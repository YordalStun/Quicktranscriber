// QuickTranscriber.exe - starts QuickTranscriber from its own folder.
//
//   - Uses the Python bundled in runtime\python (portable build) or the one set up
//     by the start script in runtime\venv. If neither exists it runs the one-time
//     setup (scripts\windows\setup.ps1) in a console window first.
//   - Runs the app in the background and shows a tray icon (Open / Quit).
//   - Starting it again while it is running just opens the app window.
//
// Build (from any OS):  GOOS=windows GOARCH=amd64 go build -ldflags "-H windowsgui -s -w" -o QuickTranscriber.exe
package main

import (
	"bytes"
	_ "embed"
	"errors"
	"fmt"
	"net"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
	"time"

	"github.com/getlantern/systray"
)

//go:embed icon.ico
var iconData []byte

const appName = "QuickTranscriber"

var (
	root     string
	port     = 8765
	child    *exec.Cmd
	childMu  sync.Mutex
	quitting bool
	logFile  *os.File
)

func main() {
	exe, err := os.Executable()
	if err != nil {
		fatal("Could not find the app folder: " + err.Error())
	}
	root = filepath.Dir(exe)
	_ = os.MkdirAll(filepath.Join(root, "data", "logs"), 0o755)
	logFile, _ = os.OpenFile(filepath.Join(root, "data", "logs", "launcher.log"), os.O_CREATE|os.O_WRONLY|os.O_TRUNC, 0o644)

	// Already running? Just bring up the window.
	for p := 8765; p < 8775; p++ {
		if isOurServer(p) {
			port = p
			openWindow()
			return
		}
	}
	port = freePort(8765)

	python := findPython()
	if python == "" {
		if !exists(filepath.Join(root, "scripts", "windows", "setup.ps1")) {
			// Usually: opened straight from the zip, so Windows copied only the exe to a temp folder.
			fatal("QuickTranscriber can't find its files next to QuickTranscriber.exe.\n\n" +
				"If you opened it from inside the zip file, extract the zip first " +
				"(right-click it → Extract All), then open QuickTranscriber.exe in the extracted folder.")
		}
		if err := runSetup(); err != nil {
			fatal("Setup did not finish:\n\n" + err.Error() + "\n\nCheck your internet connection and start QuickTranscriber again.")
		}
		python = findPython()
		if python == "" {
			fatal("Setup finished but Python was not found in the runtime folder.")
		}
	}
	if err := startApp(python); err != nil {
		fatal("QuickTranscriber could not start:\n\n" + err.Error())
	}
	systray.Run(onReady, onExit)
}

func logf(format string, a ...any) {
	if logFile != nil {
		fmt.Fprintf(logFile, time.Now().Format("15:04:05 ")+format+"\n", a...)
	}
}

func exists(p string) bool {
	_, err := os.Stat(p)
	return err == nil
}

// findPython prefers the windowless interpreter so no console window appears.
func findPython() string {
	candidates := []string{
		filepath.Join(root, "runtime", "python", "pythonw.exe"),
		filepath.Join(root, "runtime", "venv", "Scripts", "pythonw.exe"),
		filepath.Join(root, "runtime", "python", "bin", "python3"),
		filepath.Join(root, "runtime", "venv", "bin", "python3"),
	}
	for _, c := range candidates {
		if exists(c) {
			return c
		}
	}
	return ""
}

func runSetup() error {
	script := filepath.Join(root, "scripts", "windows", "setup.ps1")
	if !exists(script) {
		return errors.New("scripts\\windows\\setup.ps1 is missing - please download QuickTranscriber again")
	}
	cmd := exec.Command("powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script, "-NoLaunch")
	cmd.Dir = root
	setNewConsole(cmd)
	logf("running setup: %v", cmd.Args)
	if err := cmd.Run(); err != nil {
		return err
	}
	return nil
}

func startApp(python string) error {
	cmd := exec.Command(python, "-m", "quicktranscriber", "--port", fmt.Sprint(port))
	cmd.Dir = root
	cmd.Env = append(os.Environ(),
		"QT_ROOT="+root,
		"PYTHONUTF8=1",
		"PYTHONNOUSERSITE=1",
		"PYTHONDONTWRITEBYTECODE=1",
	)
	if logFile != nil {
		cmd.Stdout = logFile
		cmd.Stderr = logFile
	}
	hideWindow(cmd)
	logf("starting: %v", cmd.Args)
	if err := cmd.Start(); err != nil {
		return err
	}
	childMu.Lock()
	child = cmd
	childMu.Unlock()

	// If the app dies on its own (Quit button, crash), close the tray too.
	started := time.Now()
	go func() {
		err := cmd.Wait()
		logf("app exited: %v", err)
		if !quitting {
			if time.Since(started) < 20*time.Second && err != nil {
				showError("QuickTranscriber stopped while starting.\n\n" + tail(filepath.Join(root, "data", "logs", "launcher.log"), 12))
			}
			systray.Quit()
		}
	}()
	return nil
}

func onReady() {
	systray.SetIcon(iconData)
	systray.SetTitle(appName)
	systray.SetTooltip(appName + " - private meeting notes")
	open := systray.AddMenuItem("Open QuickTranscriber", "Open the app window")
	folder := systray.AddMenuItem("Open app folder", "Your meetings, voices and models are stored here")
	systray.AddSeparator()
	quit := systray.AddMenuItem("Quit", "Stop QuickTranscriber")
	go func() {
		for {
			select {
			case <-open.ClickedCh:
				openWindow()
			case <-folder.ClickedCh:
				openFolder(root)
			case <-quit.ClickedCh:
				stopApp()
				systray.Quit()
				return
			}
		}
	}()
}

func onExit() {
	stopApp()
}

func stopApp() {
	if quitting {
		return
	}
	quitting = true
	childMu.Lock()
	cmd := child
	childMu.Unlock()
	if cmd == nil || cmd.Process == nil {
		return
	}
	// Ask politely first so the AI engine is shut down cleanly.
	req, _ := http.NewRequest("POST", fmt.Sprintf("http://127.0.0.1:%d/api/shutdown", port), bytes.NewBufferString("{}"))
	req.Header.Set("X-QT", "1")
	req.Header.Set("Content-Type", "application/json")
	client := &http.Client{Timeout: 3 * time.Second}
	if resp, err := client.Do(req); err == nil {
		resp.Body.Close()
	}
	done := make(chan struct{})
	go func() {
		for i := 0; i < 80; i++ {
			if cmd.ProcessState != nil {
				break
			}
			time.Sleep(100 * time.Millisecond)
		}
		close(done)
	}()
	<-done
	if cmd.ProcessState == nil {
		_ = cmd.Process.Kill()
	}
}

func isOurServer(p int) bool {
	client := &http.Client{Timeout: 700 * time.Millisecond}
	resp, err := client.Get(fmt.Sprintf("http://127.0.0.1:%d/api/status", p))
	if err != nil {
		return false
	}
	defer resp.Body.Close()
	buf := new(bytes.Buffer)
	_, _ = buf.ReadFrom(resp.Body)
	return strings.Contains(buf.String(), `"app":"`+appName+`"`)
}

func freePort(start int) int {
	for p := start; p < start+50; p++ {
		l, err := net.Listen("tcp", fmt.Sprintf("127.0.0.1:%d", p))
		if err == nil {
			l.Close()
			return p
		}
	}
	return start
}

func url() string { return fmt.Sprintf("http://127.0.0.1:%d", port) }

// openWindow shows the app in a clean window (Edge/Chrome app mode) or the default browser.
func openWindow() {
	u := url()
	if runtime.GOOS == "windows" {
		for _, base := range []string{os.Getenv("ProgramFiles(x86)"), os.Getenv("ProgramFiles"), os.Getenv("LOCALAPPDATA")} {
			if base == "" {
				continue
			}
			for _, rel := range []string{`Microsoft\Edge\Application\msedge.exe`, `Google\Chrome\Application\chrome.exe`} {
				p := filepath.Join(base, rel)
				if exists(p) {
					if exec.Command(p, "--app="+u, "--window-size=1440,920").Start() == nil {
						return
					}
				}
			}
		}
		_ = exec.Command("rundll32", "url.dll,FileProtocolHandler", u).Start()
		return
	}
	if runtime.GOOS == "darwin" {
		_ = exec.Command("open", u).Start()
		return
	}
	_ = exec.Command("xdg-open", u).Start()
}

func openFolder(p string) {
	switch runtime.GOOS {
	case "windows":
		_ = exec.Command("explorer", p).Start()
	case "darwin":
		_ = exec.Command("open", p).Start()
	default:
		_ = exec.Command("xdg-open", p).Start()
	}
}

func tail(path string, n int) string {
	data, err := os.ReadFile(path)
	if err != nil {
		return ""
	}
	lines := strings.Split(strings.TrimSpace(string(data)), "\n")
	if len(lines) > n {
		lines = lines[len(lines)-n:]
	}
	return strings.Join(lines, "\n")
}

func fatal(msg string) {
	logf("fatal: %s", msg)
	showError(msg)
	os.Exit(1)
}
