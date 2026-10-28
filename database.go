package main

import (
	"bytes"
	"compress/zlib"
	"context"
	"encoding/base64"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"time"
)

type statement struct {
	SQL  string
	Args A
}

func sql(s string, args ...any) statement { return statement{s, args} }

type database struct {
	URL, Token string
	Client     *http.Client
}
type session struct {
	url    string
	db     *database
	baton  string
	broken bool
	active bool
	Calls  int
	Bytes  int
}

func newDatabase(raw, token string, allowTest bool) (*database, error) {
	u, e := url.Parse(strings.TrimSpace(raw))
	if e != nil {
		return nil, fmt.Errorf("invalid TURSO_DATABASE_URL")
	}
	if u.Scheme == "libsql" || u.Scheme == "turso" {
		u.Scheme = "https"
	}
	if u.Hostname() == "" || u.User != nil || u.RawQuery != "" || u.Fragment != "" || (u.Path != "" && u.Path != "/") || (u.Scheme != "https" && !(allowTest && u.Scheme == "http" && (u.Hostname() == "127.0.0.1" || u.Hostname() == "localhost"))) || token == "" || strings.ContainsAny(token, "\r\n") {
		return nil, fmt.Errorf("set a valid TURSO_DATABASE_URL and TURSO_AUTH_TOKEN; insecure URLs are refused")
	}
	u.Path = "/v2/pipeline"
	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.MaxIdleConnsPerHost = 4
	return &database{u.String(), token, &http.Client{Transport: transport, Timeout: 15 * time.Second, CheckRedirect: func(r *http.Request, v []*http.Request) error { return http.ErrUseLastResponse }}}, nil
}
func (d *database) open() *session { return &session{db: d, url: d.URL} }
func sqlValue(v any) M {
	if v == nil {
		return M{"type": "null"}
	}
	if b, ok := v.([]byte); ok {
		return M{"type": "blob", "base64": base64.StdEncoding.EncodeToString(b)}
	}
	if str, ok := v.(string); ok {
		return M{"type": "text", "value": str}
	}
	if validNumber(v) {
		n := number(v)
		if n == float64(integer(v)) {
			return M{"type": "integer", "value": strconv.FormatInt(integer(v), 10)}
		}
		return M{"type": "float", "value": n}
	}
	panic(fmt.Sprintf("unsupported SQL value %T", v))
}
func fromSQL(v any) any {
	m := obj(v)
	switch text(m["type"]) {
	case "null":
		return nil
	case "integer":
		return jsonNumber(text(m["value"]))
	case "float":
		return m["value"]
	case "text":
		return m["value"]
	case "blob":
		b, _ := base64.StdEncoding.DecodeString(text(m["base64"]))
		return b
	}
	return nil
}
func jsonNumber(s string) any {
	v, e := parse([]byte(s))
	if e != nil {
		return s
	}
	return v
}

var unavailable = errors.New("Database unavailable or save not confirmed. Retry the same save.")

func (s *session) group(ctx context.Context, stmts ...statement) ([]A, error) {
	if s.broken {
		return nil, unavailable
	}
	ops := A{}
	ending := false
	for _, st := range stmts {
		keyword := strings.ToUpper(strings.Fields(st.SQL)[0])
		if keyword == "BEGIN" {
			s.active = true
		}
		if keyword == "COMMIT" || keyword == "ROLLBACK" {
			ending = true
		}
		args := A{}
		for _, arg := range st.Args {
			args = append(args, sqlValue(arg))
		}
		ops = append(ops, M{"type": "execute", "stmt": M{"sql": st.SQL, "args": args, "want_rows": keyword == "SELECT" || keyword == "PRAGMA"}})
	}
	if ending {
		ops = append(ops, M{"type": "close"})
	}
	body := M{"requests": ops}
	if s.baton != "" {
		body["baton"] = s.baton
	}
	raw := encode(body)
	s.Bytes += len(raw)
	req, e := http.NewRequestWithContext(ctx, "POST", s.url, bytes.NewReader(raw))
	if e != nil {
		return nil, unavailable
	}
	req.Header.Set("Authorization", "Bearer "+s.db.Token)
	req.Header.Set("Content-Type", "application/json")
	s.Calls++
	resp, e := s.db.Client.Do(req)
	if e != nil {
		s.broken = true
		return nil, unavailable
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		s.broken = true
		return nil, unavailable
	}
	data, e := io.ReadAll(io.LimitReader(resp.Body, 128<<20))
	if e != nil {
		s.broken = true
		return nil, unavailable
	}
	v, e := parse(data)
	if e != nil {
		s.broken = true
		return nil, unavailable
	}
	d := obj(v)
	s.baton = text(d["baton"])
	if ending {
		s.active = false
	}
	if base := text(d["base_url"]); base != "" {
		u, err := url.Parse(base)
		original, _ := url.Parse(s.db.URL)
		trusted := u != nil && (u.Hostname() == original.Hostname() || strings.HasSuffix(original.Hostname(), ".turso.io") && strings.HasSuffix(u.Hostname(), ".turso.io"))
		if err != nil || !trusted || u.Scheme != "https" || u.User != nil || u.RawQuery != "" || u.Fragment != "" {
			s.broken = true
			return nil, unavailable
		}
		u.Path = "/v2/pipeline"
		s.url = u.String()
	}
	results := arr(d["results"])
	if len(results) != len(ops) {
		s.broken = true
		return nil, unavailable
	}
	rows := make([]A, len(stmts))
	for i, r := range results {
		rr := obj(r)
		if text(rr["type"]) != "ok" {
			return nil, unavailable
		}
		if i >= len(stmts) {
			continue
		}
		rows[i] = A{}
		for _, row := range arr(obj(obj(rr["response"])["result"])["rows"]) {
			decoded := A{}
			for _, value := range arr(row) {
				decoded = append(decoded, fromSQL(value))
			}
			rows[i] = append(rows[i], decoded)
		}
	}
	if s.active && s.baton == "" {
		s.broken = true
		return nil, unavailable
	}
	return rows, nil
}
func (s *session) commit(ctx context.Context) error { _, e := s.group(ctx, sql("COMMIT")); return e }
func (s *session) rollback() {
	if s.active && !s.broken {
		ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
		defer cancel()
		_, _ = s.group(ctx, sql("ROLLBACK"))
	}
}
func (d *database) query(ctx context.Context, stmts ...statement) ([]A, error) {
	s := d.open()
	all := append([]statement{sql("BEGIN")}, stmts...)
	rows, e := s.group(ctx, all...)
	if e != nil {
		s.rollback()
		return nil, e
	}
	if e = s.commit(ctx); e != nil {
		return nil, e
	}
	return rows[1:], nil
}
func unpack(raw string) (M, error) {
	v, e := parse([]byte(raw))
	if e != nil {
		return nil, e
	}
	m := obj(v)
	if text(m["trailmixStorage"]) == "zlib-v1" {
		b, err := base64.StdEncoding.DecodeString(text(m["payload"]))
		if err != nil {
			return nil, err
		}
		z, err := zlib.NewReader(bytes.NewReader(b))
		if err != nil {
			return nil, err
		}
		defer z.Close()
		b, err = io.ReadAll(io.LimitReader(z, 128<<20))
		if err != nil {
			return nil, err
		}
		v, err = parse(b)
		if err != nil {
			return nil, err
		}
		m = obj(v)
	}
	if m["runId"] == nil {
		return nil, fmt.Errorf("invalid legacy run")
	}
	for _, k := range append(entityKinds, "events") {
		if m[k] == nil {
			m[k] = A{}
		}
	}
	return m, nil
}

// Combine row inserts within one transaction. Bounded groups stay below older
// SQLite binding limits and keep the provider pipeline small for 50-action batches.
func coalesce(input []statement) []statement {
	out := []statement{}
	groups := map[string][]statement{}
	order := []string{}
	for _, st := range input {
		if strings.HasPrefix(st.SQL, "INSERT INTO events(") || strings.HasPrefix(st.SQL, "INSERT INTO requests(") || strings.HasPrefix(st.SQL, "INSERT INTO tm_results(") || strings.HasPrefix(st.SQL, "INSERT INTO tm_event_index(") || strings.HasPrefix(st.SQL, "INSERT INTO tm_entities(") {
			if _, ok := groups[st.SQL]; !ok {
				order = append(order, st.SQL)
			}
			groups[st.SQL] = append(groups[st.SQL], st)
		} else {
			out = append(out, st)
		}
	}
	inserts := []statement{}
	for _, query := range order {
		rows := groups[query]
		pos := strings.Index(query, "VALUES (")
		end := strings.Index(query[pos:], ")") + pos + 1
		prefix, tuple, suffix := query[:pos+7], query[pos+7:end], query[end:]
		for len(rows) > 0 {
			n := len(rows)
			if n > 100 {
				n = 100
			}
			args := A{}
			tuples := []string{}
			for _, row := range rows[:n] {
				args = append(args, row.Args...)
				tuples = append(tuples, tuple)
			}
			inserts = append(inserts, statement{prefix + strings.Join(tuples, ",") + suffix, args})
			rows = rows[n:]
		}
	}
	return append(inserts, out...)
}
