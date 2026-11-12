// Package webassets embeds the existing browser application in the Go binary.
package webassets

import (
	"embed"
	"io/fs"
	"net/http"
	"path"
	"strings"
)

//go:embed static/*
var files embed.FS

func Handler(browserSync []byte) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if strings.HasPrefix(r.URL.Path, "/api/") {
			w.Header().Set("Content-Type", "application/json; charset=utf-8")
			w.Header().Set("Cache-Control", "no-store")
			w.WriteHeader(http.StatusNotImplemented)
			_, _ = w.Write([]byte(`{"error":"此接口尚未迁移到 Go","code":"NOT_IMPLEMENTED"}`))
			return
		}
		if r.Method != http.MethodGet && r.Method != http.MethodHead {
			w.Header().Set("Allow", "GET, HEAD")
			w.WriteHeader(http.StatusMethodNotAllowed)
			return
		}
		name := strings.TrimPrefix(r.URL.Path, "/")
		if name == "" {
			name = "index.html"
		}
		if !fs.ValidPath(name) || strings.Contains(name, "/") {
			http.NotFound(w, r)
			return
		}
		data, err := files.ReadFile("static/" + name)
		if err != nil {
			http.NotFound(w, r)
			return
		}
		w.Header().Set("Cache-Control", "no-store")
		w.Header().Set("X-Content-Type-Options", "nosniff")
		switch path.Ext(name) {
		case ".html":
			w.Header().Set("Content-Type", "text/html; charset=utf-8")
		case ".css":
			w.Header().Set("Content-Type", "text/css; charset=utf-8")
		case ".js":
			w.Header().Set("Content-Type", "text/javascript; charset=utf-8")
		}
		if name == "live-sync.js" {
			data = append(append(data, '\n'), browserSync...)
		}
		if r.Method == http.MethodGet {
			_, _ = w.Write(data)
		}
	})
}
