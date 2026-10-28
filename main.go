package main

import (
	"context"
	"crypto/subtle"
	_ "embed"
	"flag"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"net/url"
	"os"
	"os/signal"
	"strings"
	"sync"
	"syscall"
	"time"
)

//go:embed trailmix.html
var frontend []byte

func env(key, fallback string) string {
	if v, ok := os.LookupEnv(key); ok {
		return v
	}
	return fallback
}

type application struct {
	stopping                  <-chan struct{}
	engine                    *engine
	access                    string
	requireCode, secureCookie bool
	loginMu                   sync.Mutex
	failures                  int
	blocked                   time.Time
}

func (a *application) authorized(r *http.Request) bool {
	if !a.requireCode {
		return true
	}
	cookie, e := r.Cookie("trailmix")
	return e == nil && subtle.ConstantTimeCompare([]byte(cookie.Value), []byte(a.engine.cookie)) == 1
}
func headers(w http.ResponseWriter) {
	h := w.Header()
	h.Set("Cache-Control", "no-store")
	h.Set("X-Content-Type-Options", "nosniff")
	h.Set("X-Frame-Options", "DENY")
	h.Set("Referrer-Policy", "same-origin")
	h.Set("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'")
	h.Set("X-TrailMix-Backend", "go-normalized-v1")
}
func respond(w http.ResponseWriter, r *http.Request, code int, body any) {
	raw := encode(body)
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Content-Length", fmt.Sprint(len(raw)))
	w.WriteHeader(code)
	if r.Method != "HEAD" {
		_, _ = w.Write(raw)
	}
}
func failed(w http.ResponseWriter, r *http.Request, err error) {
	if isProblem(err) {
		respond(w, r, 409, M{"error": err.Error()})
		return
	}
	log.Printf("Save/read not confirmed: %v", err)
	respond(w, r, 503, M{"error": "Database unavailable or save not confirmed. Retry the same save.", "retryable": true})
}
func (a *application) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	headers(w)
	defer func() {
		if v := recover(); v != nil {
			log.Printf("Request failed: %T", v)
			respond(w, r, 500, M{"error": "Save not confirmed. Retry the same save.", "retryable": true})
		}
	}()
	if (r.Method == "GET" || r.Method == "HEAD") && r.URL.Path == "/healthz" {
		respond(w, r, 200, M{"ok": true, "backend": "go-normalized-v1"})
		return
	}
	if (r.Method == "GET" || r.Method == "HEAD") && (r.URL.Path == "/" || r.URL.Path == "/index.html") {
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		w.Header().Set("Content-Length", fmt.Sprint(len(frontend)))
		w.WriteHeader(200)
		if r.Method != "HEAD" {
			_, _ = w.Write(frontend)
		}
		return
	}
	if r.Method == "HEAD" {
		respond(w, r, 404, M{"error": "Not found."})
		return
	}
	if r.Method == "POST" {
		a.post(w, r)
		return
	}
	if r.Method != "GET" {
		respond(w, r, 405, M{"error": "Method not allowed."})
		return
	}
	if !a.authorized(r) {
		respond(w, r, 401, M{"error": "Enter the team access code to connect."})
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 45*time.Second)
	defer cancel()
	switch r.URL.Path {
	case "/api/state":
		if err := a.engine.ensure(ctx); err != nil {
			failed(w, r, err)
			return
		}
		a.engine.touch(r.URL.Query().Get("deviceId"), "", "")
		respond(w, r, 200, a.engine.view())
	case "/api/runs":
		list, err := runList(ctx, a.engine.db)
		if err != nil {
			failed(w, r, err)
			return
		}
		respond(w, r, 200, list)
	case "/api/stream":
		a.stream(w, r)
	case "/api/export":
		a.export(w, r, ctx)
	default:
		respond(w, r, 404, M{"error": "Not found."})
	}
}
func (a *application) post(w http.ResponseWriter, r *http.Request) {
	if r.Header.Get("X-TrailMix") != "1" || strings.Split(r.Header.Get("Content-Type"), ";")[0] != "application/json" {
		respond(w, r, 403, M{"error": "Invalid request origin or content type."})
		return
	}
	if origin := r.Header.Get("Origin"); origin != "" {
		u, err := url.Parse(origin)
		if err != nil || u.Host != r.Host {
			respond(w, r, 403, M{"error": "Cross-origin writes are not allowed."})
			return
		}
	}
	reader := http.MaxBytesReader(w, r.Body, 65536)
	raw, err := io.ReadAll(reader)
	if err != nil || len(raw) == 0 {
		respond(w, r, 409, M{"error": "Invalid request size."})
		return
	}
	value, err := parse(raw)
	req, ok := value.(map[string]any)
	if err != nil || !ok {
		respond(w, r, 409, M{"error": "Invalid JSON object."})
		return
	}
	if r.URL.Path == "/api/login" {
		a.login(w, r, req)
		return
	}
	if !a.authorized(r) {
		respond(w, r, 401, M{"error": "Enter the team access code to connect."})
		return
	}
	// Finish a bounded transaction even when a browser closes its connection.
	// Lost confirmations retain the original request IDs for duplicate-safe retry.
	ctx, cancel := context.WithTimeout(context.Background(), 45*time.Second)
	defer cancel()
	if r.URL.Path == "/api/device/hello" {
		state, err := a.engine.hello(ctx, req)
		if err != nil {
			failed(w, r, err)
			return
		}
		if r.Header.Get("X-TrailMix-Sync") == "delta-v1" {
			respond(w, r, 200, M{"delta": M{"runId": state["runId"], "baseRevision": state["revision"], "revision": state["revision"], "core": core(state), "entities": M{"devices": M{"upsert": state["devices"], "remove": A{}}}, "eventsAppend": A{}}})
		} else {
			respond(w, r, 200, state)
		}
		return
	}
	batch := r.URL.Path == "/api/actions"
	if !batch && r.URL.Path != "/api/action" {
		respond(w, r, 404, M{"error": "Not found."})
		return
	}
	requests := A{req}
	if batch {
		requests = arr(req["commands"])
	}
	reply, err := a.engine.commands(ctx, requests, batch)
	if err != nil {
		failed(w, r, err)
		return
	}
	if !batch {
		first := obj(arr(reply["results"])[0])
		reply["event"] = first["event"]
		reply["duplicate"] = first["duplicate"]
		delete(reply, "results")
	}
	if r.Header.Get("X-TrailMix-Sync") != "delta-v1" {
		reply["state"] = a.engine.view()
		delete(reply, "delta")
	}
	log.Printf("Batch saved: actions=%d save_seconds=%.6f database_trips=%v database_request_bytes=%v", len(requests), number(obj(reply["timings"])["total_seconds"]), reply["databaseTrips"], reply["databaseRequestBytes"])
	respond(w, r, 200, reply)
}
func (a *application) login(w http.ResponseWriter, r *http.Request, req M) {
	if !a.requireCode {
		respond(w, r, 200, M{"ok": true})
		return
	}
	a.loginMu.Lock()
	defer a.loginMu.Unlock()
	if time.Now().Before(a.blocked) {
		respond(w, r, 429, M{"error": "Too many access-code attempts. Try again in a minute."})
		return
	}
	if subtle.ConstantTimeCompare([]byte(text(req["code"])), []byte(a.access)) != 1 {
		a.failures++
		if a.failures >= 20 {
			a.failures = 0
			a.blocked = time.Now().Add(time.Minute)
		}
		respond(w, r, 401, M{"error": "Incorrect team access code."})
		return
	}
	http.SetCookie(w, &http.Cookie{Name: "trailmix", Value: a.engine.cookie, Path: "/", HttpOnly: true, Secure: a.secureCookie, SameSite: http.SameSiteStrictMode})
	respond(w, r, 200, M{"ok": true})
}
func (e *engine) ensure(ctx context.Context) error {
	e.mu.RLock()
	dirty := e.dirty
	e.mu.RUnlock()
	if !dirty {
		return nil
	}
	e.writeMu.Lock()
	defer e.writeMu.Unlock()
	e.mu.RLock()
	dirty = e.dirty
	e.mu.RUnlock()
	if dirty {
		return e.reload(ctx)
	}
	return nil
}
func (a *application) stream(w http.ResponseWriter, r *http.Request) {
	if _, ok := w.(http.Flusher); !ok {
		respond(w, r, 500, M{"error": "Streaming unavailable."})
		return
	}
	w.Header().Set("Content-Type", "text/event-stream")
	w.Header().Set("Cache-Control", "no-cache, no-transform")
	w.Header().Set("X-Accel-Buffering", "no")
	w.WriteHeader(200)
	controller := http.NewResponseController(w)
	emit := func(kind string, data any) error {
		_ = controller.SetWriteDeadline(time.Now().Add(15 * time.Second))
		_, err := fmt.Fprintf(w, "event: %s\ndata: %s\n\n", kind, encode(data))
		if err == nil {
			err = controller.Flush()
		}
		return err
	}
	deltaMode := r.URL.Query().Get("sync") == "delta-v1"
	did := r.URL.Query().Get("deviceId")
	e := a.engine
	e.mu.RLock()
	seen := e.serial
	initial := M{}
	for k, v := range e.state {
		initial[k] = v
	}
	initial["events"] = e.history
	initial["devices"] = e.devices(e.state)
	e.mu.RUnlock()
	if emit("state", initial) != nil {
		return
	}
	ticker := time.NewTicker(20 * time.Second)
	defer ticker.Stop()
	for {
		e.mu.RLock()
		current := e.serial
		notify := e.notify
		changes := append([]change{}, e.changes...)
		e.mu.RUnlock()
		if current != seen {
			missed := len(changes) == 0 || changes[0].Serial > seen+1
			if !deltaMode || missed {
				if emit("state", e.view()) != nil {
					return
				}
				seen = current
			} else {
				for _, ch := range changes {
					if ch.Serial > seen {
						if emit("patch", ch.Delta) != nil {
							return
						}
						seen = ch.Serial
					}
				}
			}
			continue
		}
		select {
		case <-r.Context().Done():
			return
		case <-a.stopping:
			return
		case <-notify:
			continue
		case <-ticker.C:
			e.touch(did, "", "")
			if emit("ping", M{"at": now(), "devices": e.view()["devices"]}) != nil {
				return
			}
		}
	}
}
func main() {
	host := flag.String("host", "0.0.0.0", "Listen address")
	port := flag.String("port", env("PORT", "8080"), "Listen port")
	flag.Parse()
	db, err := newDatabase(env("TURSO_DATABASE_URL", ""), env("TURSO_AUTH_TOKEN", ""), env("TRAILMIX_TEST_DB", "0") == "1")
	if err != nil {
		log.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Minute)
	e, err := newEngine(ctx, db, env("TRAILMIX_SAFETY_PIN", ""))
	if err == nil {
		err = e.bootstrap(ctx)
	}
	cancel()
	if err != nil {
		log.Fatal(err)
	}
	stopping := make(chan struct{})
	a := &application{stopping: stopping, engine: e, access: env("TRAILMIX_ACCESS_CODE", ""), requireCode: env("TRAILMIX_REQUIRE_ACCESS_CODE", "0") == "1", secureCookie: env("TRAILMIX_HTTPS", "0") == "1"}
	if a.requireCode && a.access == "" {
		log.Fatal("Set TRAILMIX_ACCESS_CODE when sign-in is required")
	}
	ln, err := net.Listen("tcp", net.JoinHostPort(*host, *port))
	if err != nil {
		log.Fatal(err)
	}
	fmt.Printf("TrailMix Go is running at http://%s\nDatabase: Turso normalized records\nSafety Rover PIN: saved; unchanged across restarts\n", ln.Addr())
	server := &http.Server{Handler: a, ReadHeaderTimeout: 10 * time.Second, ReadTimeout: 20 * time.Second, IdleTimeout: 65 * time.Second, MaxHeaderBytes: 16 << 10}
	shutdown := make(chan os.Signal, 1)
	signal.Notify(shutdown, os.Interrupt, syscall.SIGTERM)
	drained := make(chan struct{})
	go func() {
		<-shutdown
		close(stopping)
		ctx, cancel := context.WithTimeout(context.Background(), 50*time.Second)
		defer cancel()
		_ = server.Shutdown(ctx)
		close(drained)
	}()
	if err = server.Serve(ln); err != nil && err != http.ErrServerClosed {
		log.Fatal(err)
	}
	<-drained
}
