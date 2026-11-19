package main

import (
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
)

func privateCollectionToken(t *testing.T, content string) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "private-worker-token")
	if err := os.WriteFile(path, []byte(content), 0o600); err != nil {
		t.Fatal(err)
	}
	return path
}

func TestResolveCollectionTokenModes(t *testing.T) {
	token := strings.Repeat("s", 48)
	file := privateCollectionToken(t, token)
	tests := []struct {
		name   string
		config collectionTokenConfig
		want   string
		valid  bool
	}{
		{"disabled-local", collectionTokenConfig{ListenHost: "127.0.0.1"}, "", true},
		{"disabled-production", collectionTokenConfig{ListenHost: "0.0.0.0", PublicOrigin: "http://42.192.45.194", CloudBase: true}, "", true},
		{"existing-local-env", collectionTokenConfig{EnvironmentToken: token, ListenHost: "127.0.0.1"}, token, true},
		{"ipv6-local-env", collectionTokenConfig{EnvironmentToken: token, ListenHost: "::1"}, token, true},
		{"public-env-denied", collectionTokenConfig{EnvironmentToken: token, ListenHost: "0.0.0.0", PublicOrigin: "http://42.192.45.194"}, "", false},
		{"origin-env-denied", collectionTokenConfig{EnvironmentToken: token, ListenHost: "127.0.0.1", PublicOrigin: "http://127.0.0.1:8080"}, "", false},
		{"cloudbase-env-denied", collectionTokenConfig{EnvironmentToken: token, ListenHost: "127.0.0.1", CloudBase: true}, "", false},
		{"local-file", collectionTokenConfig{FilePath: file, ListenHost: "127.0.0.1"}, token, true},
		{"public-file", collectionTokenConfig{FilePath: file, ListenHost: "0.0.0.0", PublicOrigin: "http://42.192.45.194"}, token, true},
		{"cloudbase-file", collectionTokenConfig{FilePath: file, ListenHost: "0.0.0.0", PublicOrigin: "http://42.192.45.194", CloudBase: true}, token, true},
		{"https-file", collectionTokenConfig{FilePath: file, ListenHost: "127.0.0.1", PublicOrigin: "https://board.example.test", CloudBase: true}, token, true},
		{"conflicting-sources", collectionTokenConfig{FilePath: file, EnvironmentToken: token, ListenHost: "127.0.0.1"}, "", false},
		{"legacy-env", collectionTokenConfig{EnvironmentToken: token, ListenHost: "127.0.0.1", Legacy: true}, "", false},
		{"legacy-file", collectionTokenConfig{FilePath: file, ListenHost: "127.0.0.1", Legacy: true}, "", false},
		{"initialize-file", collectionTokenConfig{FilePath: file, ListenHost: "127.0.0.1", Initialize: true}, "", false},
		{"public-origin-required", collectionTokenConfig{FilePath: file, ListenHost: "0.0.0.0"}, "", false},
		{"cloudbase-origin-required", collectionTokenConfig{FilePath: file, ListenHost: "127.0.0.1", CloudBase: true}, "", false},
		{"invalid-origin", collectionTokenConfig{FilePath: file, ListenHost: "127.0.0.1", PublicOrigin: "http://board.example.test/path"}, "", false},
		{"invalid-env-token", collectionTokenConfig{EnvironmentToken: "short", ListenHost: "127.0.0.1"}, "", false},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			got, err := resolveCollectionToken(test.config)
			if (err == nil) != test.valid || got != test.want {
				t.Fatal("collection mode activation did not match the expected boundary")
			}
			if err != nil && (strings.Contains(err.Error(), token) || strings.Contains(err.Error(), file)) {
				t.Fatal("collection error included private input")
			}
		})
	}
}

func TestReadCollectionTokenFileFormatAndBoundaries(t *testing.T) {
	tests := []struct {
		name, content, want string
		valid               bool
	}{
		{"minimum", strings.Repeat("m", 32), strings.Repeat("m", 32), true},
		{"maximum", strings.Repeat("x", 256), strings.Repeat("x", 256), true},
		{"line-ending", strings.Repeat("s", 48) + "\n", strings.Repeat("s", 48), true},
		{"windows-line-ending", strings.Repeat("s", 256) + "\r\n", strings.Repeat("s", 256), true},
		{"short", strings.Repeat("s", 31), "", false},
		{"oversized", strings.Repeat("s", 259), "", false},
		{"long-token", strings.Repeat("s", 257), "", false},
		{"space", strings.Repeat("s", 31) + " ", "", false},
		{"unicode", strings.Repeat("s", 32) + "密", "", false},
		{"nul", strings.Repeat("s", 32) + "\x00", "", false},
		{"multiple-lines", strings.Repeat("s", 32) + "\nother", "", false},
		{"extra-line", strings.Repeat("s", 32) + "\n\n", "", false},
		{"bare-cr", strings.Repeat("s", 32) + "\r", "", false},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			path := privateCollectionToken(t, test.content)
			got, err := readCollectionTokenFile(path)
			if (err == nil) != test.valid || got != test.want {
				t.Fatal("private collection file format was not enforced")
			}
			if err != nil && strings.Contains(err.Error(), path) {
				t.Fatal("collection error included a private path")
			}
		})
	}
	for name, path := range map[string]string{"relative": "worker-token", "missing": filepath.Join(t.TempDir(), "missing"), "directory": t.TempDir()} {
		t.Run(name, func(t *testing.T) {
			if token, err := readCollectionTokenFile(path); err == nil || token != "" || strings.Contains(err.Error(), path) {
				t.Fatal("non-private file was accepted or its path disclosed")
			}
		})
	}
}

func TestReadCollectionTokenFileRejectsSymlink(t *testing.T) {
	target := privateCollectionToken(t, strings.Repeat("s", 48))
	link := filepath.Join(t.TempDir(), "worker-link")
	if err := os.Symlink(target, link); err != nil {
		t.Skip("symbolic link creation is unavailable on this test host")
	}
	if token, err := readCollectionTokenFile(link); err == nil || token != "" {
		t.Fatal("symbolic link token source was accepted")
	}
}

func TestReadCollectionTokenFileUnixPermissions(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("Unix mode-bit enforcement requires a Unix test host")
	}
	for _, mode := range []os.FileMode{0o644, 0o640, 0o604, 0o666} {
		path := privateCollectionToken(t, strings.Repeat("s", 48))
		if err := os.Chmod(path, mode); err != nil {
			t.Fatal(err)
		}
		if token, err := readCollectionTokenFile(path); err == nil || token != "" {
			t.Fatal("group- or world-accessible token source was accepted")
		}
	}
	path := privateCollectionToken(t, strings.Repeat("s", 48))
	if err := os.Chmod(path, 0o400); err != nil {
		t.Fatal(err)
	}
	if _, err := readCollectionTokenFile(path); err != nil {
		t.Fatal("read-only systemd credential file was rejected")
	}
}
