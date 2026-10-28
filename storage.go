package main

import (
	"context"
	"fmt"
	"sort"
	"strconv"
)

var schema = []string{

	`CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY,state TEXT NOT NULL,archived INTEGER NOT NULL DEFAULT 0)`,
	`CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)`,
	`CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT,id TEXT UNIQUE NOT NULL,run_id TEXT NOT NULL,at INTEGER NOT NULL,operator TEXT NOT NULL,action TEXT NOT NULL,payload TEXT NOT NULL,result TEXT NOT NULL)`,
	`CREATE TABLE IF NOT EXISTS requests(id TEXT PRIMARY KEY,digest TEXT NOT NULL,run_id TEXT NOT NULL)`,
	`CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT,'Event history is append-only'); END`,
	`CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT,'Event history is append-only'); END`,
	`CREATE INDEX IF NOT EXISTS events_run_seq ON events(run_id,seq)`,
	`CREATE TABLE IF NOT EXISTS tm_runs(id TEXT PRIMARY KEY,revision INTEGER NOT NULL,core TEXT NOT NULL,archived INTEGER NOT NULL DEFAULT 0)`,
	`CREATE TABLE IF NOT EXISTS tm_entities(run_id TEXT NOT NULL,kind TEXT NOT NULL,id TEXT NOT NULL,position INTEGER NOT NULL,body TEXT NOT NULL,PRIMARY KEY(run_id,kind,id))`,
	`CREATE TABLE IF NOT EXISTS tm_event_index(run_id TEXT NOT NULL,id TEXT PRIMARY KEY,revision INTEGER NOT NULL,summary TEXT NOT NULL)`,
	`CREATE INDEX IF NOT EXISTS tm_event_run_revision ON tm_event_index(run_id,revision)`,
	`CREATE TABLE IF NOT EXISTS tm_results(request_id TEXT PRIMARY KEY,result TEXT NOT NULL)`,
	`CREATE TRIGGER IF NOT EXISTS tm_legacy_runs_readonly_update BEFORE UPDATE ON runs WHEN (SELECT value FROM meta WHERE key='tm_storage_version')='1' BEGIN SELECT RAISE(ABORT,'Database migrated to Go; legacy runs are read-only'); END`,
	`CREATE TRIGGER IF NOT EXISTS tm_legacy_runs_readonly_insert BEFORE INSERT ON runs WHEN (SELECT value FROM meta WHERE key='tm_storage_version')='1' BEGIN SELECT RAISE(ABORT,'Database migrated to Go; legacy runs are read-only'); END`,
	`CREATE TRIGGER IF NOT EXISTS tm_legacy_runs_readonly_delete BEFORE DELETE ON runs WHEN (SELECT value FROM meta WHERE key='tm_storage_version')='1' BEGIN SELECT RAISE(ABORT,'Database migrated to Go; legacy runs are read-only'); END`,
}

func initStorage(ctx context.Context, db *database, pin string) (string, string, error) {
	sess := db.open()
	defer sess.rollback()
	stmts := []statement{sql("BEGIN IMMEDIATE")}
	for _, q := range schema {
		stmts = append(stmts, sql(q))
	}
	if _, e := sess.group(ctx, stmts...); e != nil {
		return "", "", e
	}
	rows, e := sess.group(ctx, sql("SELECT key,value FROM meta"), sql("SELECT id,state,archived FROM runs WHERE id NOT IN (SELECT id FROM tm_runs) ORDER BY rowid"), sql("SELECT id FROM tm_runs"))
	if e != nil {
		return "", "", e
	}
	meta := map[string]string{}
	for _, v := range rows[0] {
		r := arr(v)
		meta[text(r[0])] = text(r[1])
	}
	known := map[string]bool{}
	for _, v := range rows[2] {
		known[text(arr(v)[0])] = true
	}
	writes := []statement{}
	// Copy historical snapshots once. Existing append-only audit rows and request
	// digests are preserved; no legacy run snapshot is overwritten or deleted.
	for _, v := range rows[1] {
		r := arr(v)
		run := text(r[0])
		if known[run] {
			continue
		}
		state, err := unpack(text(r[1]))
		if err != nil {
			return "", "", fmt.Errorf("legacy run could not be read: %w", err)
		}
		existingRows, err := sess.group(ctx, sql("SELECT id FROM events WHERE run_id=?", run))
		if err != nil {
			return "", "", err
		}
		existing := map[string]bool{}
		for _, v := range existingRows[0] {
			existing[text(arr(v)[0])] = true
		}
		writes = append(writes, insertRun(state, integer(r[2])))
		writes = append(writes, entityWrites(nil, state)...)
		for i, v := range arr(state["events"]) {
			event := obj(v)
			revision := integer(event["revision"])
			if revision == 0 {
				revision = int64(i + 1)
			}
			writes = append(writes, sql("INSERT INTO tm_event_index(run_id,id,revision,summary) VALUES (?,?,?,?)", run, event["id"], revision, string(encode(eventSummary(event)))))
			if rid := text(event["requestId"]); rid != "" {
				writes = append(writes, sql("INSERT OR IGNORE INTO tm_results(request_id,result) VALUES (?,?)", rid, string(encode(eventSummary(event)))))
			}
			if !existing[text(event["id"])] {
				writes = append(writes, auditWrite(run, event))
			}
		}
		known[run] = true
		// Bound each pipeline upload while keeping the same transaction/baton.
		for len(writes) > 200 {
			if _, err = sess.group(ctx, writes[:200]...); err != nil {
				return "", "", err
			}
			writes = writes[200:]
		}
	}
	if meta["current"] == "" {
		state := fresh(uuid, now())
		meta["current"] = text(state["runId"])
		writes = append(writes, insertRun(state, 0))
		writes = append(writes, entityWrites(nil, state)...)
		writes = append(writes, sql("INSERT INTO meta(key,value) VALUES ('current',?)", meta["current"]))
	}
	if pin == "" {
		pin = meta["safety_pin"]
	}
	if pin == "" {
		pin = uuid()[:8]
		fmt.Println("Safety Rover PIN:", pin)
	}
	cookie := meta["auth_cookie"]
	if cookie == "" {
		cookie = uuid() + uuid()
	}
	for k, v := range map[string]string{"safety_pin": pin, "auth_cookie": cookie, "tm_storage_version": "1"} {
		writes = append(writes, sql("INSERT INTO meta(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", k, v))
	}
	for len(writes) > 0 {
		n := len(writes)
		if n > 200 {
			n = 200
		}
		if _, e = sess.group(ctx, writes[:n]...); e != nil {
			return "", "", e
		}
		writes = writes[n:]
	}
	if e = sess.commit(ctx); e != nil {
		return "", "", e
	}
	return pin, cookie, nil
}
func insertRun(s M, archived int64) statement {
	return sql("INSERT INTO tm_runs(id,revision,core,archived) VALUES (?,?,?,?)", s["runId"], s["revision"], string(encode(core(s))), archived)
}
func auditWrite(run string, event M) statement {
	return sql("INSERT INTO events(id,run_id,at,operator,action,payload,result) VALUES (?,?,?,?,?,?,?)", event["id"], run, event["at"], event["operator"], event["action"], string(encode(event["data"])), string(encode(event)))
}
func entityWrites(before, after M) []statement {
	writes := []statement{}
	run := text(after["runId"])
	for _, kind := range entityKinds {
		old := map[string]any{}
		for _, v := range arr(before[kind]) {
			old[text(obj(v)["id"])] = v
		}
		for pos, v := range arr(after[kind]) {
			m := obj(v)
			id := text(m["id"])
			if prev, ok := old[id]; !ok || !equal(prev, m) {
				writes = append(writes, sql("INSERT INTO tm_entities(run_id,kind,id,position,body) VALUES (?,?,?,?,?) ON CONFLICT(run_id,kind,id) DO UPDATE SET position=excluded.position,body=excluded.body", run, kind, id, pos, string(encode(m))))
			}
			delete(old, id)
		}
		for id := range old {
			writes = append(writes, sql("DELETE FROM tm_entities WHERE run_id=? AND kind=? AND id=?", run, kind, id))
		}
	}
	return writes
}
func loadRun(ctx context.Context, db *database, run string, history bool) (M, A, error) {
	mode := 0
	if history {
		mode = 1
	}
	return loadSnapshot(ctx, db, run, mode)
}

// Header, entities and audit are read from one database snapshot so exports
// cannot mix a pre-commit header with post-commit bags or event history.
func loadSnapshot(ctx context.Context, db *database, run string, eventMode int) (M, A, error) {
	expression := "?"
	args := A{run}
	if run == "" {
		expression = "(SELECT value FROM meta WHERE key='current')"
		args = A{}
	}
	stmts := []statement{sql("BEGIN"), statement{"SELECT id,core FROM tm_runs WHERE id=" + expression, args}, statement{"SELECT kind,body FROM tm_entities WHERE run_id=" + expression + " ORDER BY kind,position", args}}
	if eventMode == 1 {
		stmts = append(stmts, statement{"SELECT summary FROM tm_event_index WHERE run_id=" + expression + " ORDER BY revision,rowid", args})
	}
	if eventMode == 2 {
		stmts = append(stmts, statement{"SELECT result FROM events WHERE run_id=" + expression + " ORDER BY seq", args})
	}
	sess := db.open()
	defer sess.rollback()
	rows, err := sess.group(ctx, stmts...)
	if err != nil {
		return nil, nil, err
	}
	if len(rows[1]) == 0 {
		return nil, nil, problem{"Run not found."}
	}
	if err = sess.commit(ctx); err != nil {
		return nil, nil, err
	}
	value, err := parse([]byte(text(arr(rows[1][0])[1])))
	if err != nil {
		return nil, nil, err
	}
	state := obj(value)
	for _, kind := range entityKinds {
		state[kind] = A{}
	}
	for _, row := range rows[2] {
		r := arr(row)
		v, err := parse([]byte(text(r[1])))
		if err != nil {
			return nil, nil, err
		}
		add(state, text(r[0]), v)
	}
	events := A{}
	if eventMode > 0 {
		for _, row := range rows[3] {
			v, err := parse([]byte(text(arr(row)[0])))
			if err != nil {
				return nil, nil, err
			}
			events = append(events, v)
		}
	}
	state["events"] = A{}
	return state, events, nil
}

func stateDelta(before, after M, events A) M {
	if before == nil || !same(before["runId"], after["runId"]) {
		return M{"reset": after, "eventsAppend": events}
	}
	changes := M{}
	for _, kind := range entityKinds {
		old := map[string]any{}
		for _, v := range arr(before[kind]) {
			old[text(obj(v)["id"])] = v
		}
		upsert, remove := A{}, A{}
		for _, v := range arr(after[kind]) {
			id := text(obj(v)["id"])
			if prev, ok := old[id]; !ok || !equal(prev, v) {
				upsert = append(upsert, v)
			}
			delete(old, id)
		}
		keys := []string{}
		for id := range old {
			keys = append(keys, id)
		}
		sort.Strings(keys)
		for _, id := range keys {
			remove = append(remove, id)
		}
		if len(upsert) > 0 || len(remove) > 0 {
			changes[kind] = M{"upsert": upsert, "remove": remove}
		}
	}
	return M{"runId": after["runId"], "baseRevision": before["revision"], "revision": after["revision"], "core": core(after), "entities": changes, "eventsAppend": events}
}
func runList(ctx context.Context, db *database) (A, error) {
	rows, e := db.query(ctx, sql("SELECT id,core,archived,(SELECT count(*) FROM tm_entities e WHERE e.run_id=r.id AND e.kind='bags'),(SELECT count(*) FROM tm_event_index e WHERE e.run_id=r.id) FROM tm_runs r ORDER BY rowid DESC"))
	if e != nil {
		return nil, e
	}
	out := A{}
	for _, v := range rows[0] {
		r := arr(v)
		v, _ := parse([]byte(text(r[1])))
		s := obj(v)
		out = append(out, M{"id": r[0], "name": s["batch"], "started": s["started"], "archived": integer(r[2]) != 0, "bags": r[3], "events": r[4]})
	}
	return out, nil
}
func entityID(v any) string { return strconv.FormatInt(integer(v), 10) }
