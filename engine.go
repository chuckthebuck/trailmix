package main

import (
	"context"
	"crypto/subtle"
	"errors"
	"strings"
	"sync"
	"time"
)

type change struct {
	Serial   uint64
	Run      string
	Revision int64
	Delta    M
}
type engine struct {
	db          *database
	pin, cookie string
	writeMu     sync.Mutex
	mu          sync.RWMutex
	state       M
	history     A
	remap       map[string]any
	dirty       bool
	serial      uint64
	changes     []change
	notify      chan struct{}
	presenceMu  sync.Mutex
	presence    map[string]M
}

func newEngine(ctx context.Context, db *database, pin string) (*engine, error) {
	pin, cookie, err := initStorage(ctx, db, pin)
	if err != nil {
		return nil, err
	}
	s, events, err := loadRun(ctx, db, "", true)
	if err != nil {
		return nil, err
	}
	e := &engine{db: db, pin: pin, cookie: cookie, state: s, history: events, remap: map[string]any{}, notify: make(chan struct{}), presence: map[string]M{}}
	for _, v := range events {
		e.indexRemap(obj(v))
	}
	return e, nil
}
func (e *engine) indexRemap(event M) {
	rid := text(event["requestId"])
	if rid == "" {
		return
	}
	action := text(event["action"])
	c := obj(event["changes"])
	if action == "fill" && len(arr(event["bags"])) > 0 {
		e.remap["pending-"+rid] = arr(event["bags"])[0]
	}
	if action == "labelPickup" {
		e.remap["pending-"+rid] = c["movementId"]
	}
	if action == "routePickup" && len(arr(c["deliveryIds"])) > 0 {
		e.remap["pending-"+rid] = arr(c["deliveryIds"])[0]
		if c["movementId"] != nil {
			e.remap["pending-move-"+rid] = c["movementId"]
		}
	}
}
func (e *engine) touch(did, mode, op string) {
	e.presenceMu.Lock()
	defer e.presenceMu.Unlock()
	p := e.presence[did]
	if p == nil {
		p = M{}
		e.presence[did] = p
	}
	p["seen"] = now()
	if mode != "" {
		p["mode"] = mode
	}
	if op != "" {
		p["operator"] = op
	}
}
func (e *engine) devices(source M) A {
	ds := arr(clone(source["devices"]))
	e.presenceMu.Lock()
	defer e.presenceMu.Unlock()
	for _, v := range ds {
		d := obj(v)
		p := e.presence[text(d["id"])]
		seen := d["createdAt"]
		if p != nil {
			seen = p["seen"]
		}
		merge(d, M{"lastSeen": seen, "online": p != nil && now()-integer(seen) < 45000, "observedMode": text(p["mode"]), "observedOperator": text(p["operator"])})
	}
	return ds
}
func (e *engine) view() M {
	e.mu.RLock()
	defer e.mu.RUnlock()
	s := M{}
	for k, v := range e.state {
		s[k] = v
	}
	s["events"] = e.history
	s["devices"] = e.devices(e.state)
	return s
}
func (e *engine) invalidate() { e.mu.Lock(); e.dirty = true; e.mu.Unlock() }
func (e *engine) reload(ctx context.Context) error {
	s, events, err := loadRun(ctx, e.db, "", true)
	if err != nil {
		return err
	}
	e.mu.Lock()
	e.state = s
	e.history = events
	e.remap = map[string]any{}
	for _, v := range events {
		e.indexRemap(obj(v))
	}
	e.dirty = false
	e.serial++
	e.changes = nil
	close(e.notify)
	e.notify = make(chan struct{})
	e.mu.Unlock()
	return nil
}
func mapFromIDs(s M, bagIDs any) A {
	selected := A{}
	if len(arr(bagIDs)) == 0 {
		return selected
	}
	wanted := idSet(bagIDs)
	for _, v := range arr(s["bags"]) {
		if wanted[text(obj(v)["id"])] {
			selected = append(selected, clone(v))
		}
	}
	return selected
}
func makeEvent(before, after M, req, args, result M, at int64, id string) M {
	action, op := text(req["action"]), strings.TrimSpace(text(req["operator"]))
	bs := arr(result["bags"])
	qty := result["qty"]
	if qty == nil {
		qty = len(bs)
	}
	changes := obj(clone(result))
	kind := changes["type"]
	delete(changes, "type")
	payload := obj(clone(args))
	delete(payload, "safetyPin")
	p := person(before, op)
	var pid any
	assignments := A{}
	if p != nil {
		pid = p["id"]
		assignments = arr(clone(p["assignments"]))
	}
	d := find(after, "devices", req["deviceId"])
	var did, dname any
	if d != nil {
		did = d["id"]
		dname = d["name"]
	}
	return M{"id": id, "at": at, "operator": op, "requestId": req["requestId"], "recordedAt": req["recordedAt"], "recordedStation": req["recordedStation"], "type": kind, "action": action, "qty": qty, "bags": bs, "detail": result["detail"], "data": payload, "changes": changes, "revision": after["revision"], "runId": before["runId"], "personId": pid, "assignments": assignments, "deviceId": did, "deviceName": dname, "before": mapFromIDs(before, bs), "after": mapFromIDs(after, bs)}
}
func nextRun(old M, args M, op string, at int64) M {
	n := fresh(uuid, at)
	n["batch"] = strings.TrimSpace(text(args["name"]))
	choice := text(args["configId"])
	if choice == "" || choice == "current" {
		n["people"] = clone(old["people"])
		n["devices"] = clone(old["devices"])
		n["stations"] = A{}
		for _, v := range arr(old["stations"]) {
			add(n, "stations", M{"id": obj(v)["id"], "empty": 0, "batch": ""})
		}
	} else {
		def := obj(find(old, "configs", choice)["definition"])
		n["stations"] = A{}
		for _, v := range arr(def["stations"]) {
			add(n, "stations", M{"id": obj(v)["id"], "empty": 0, "batch": ""})
		}
		n["people"] = clone(def["people"])
		n["devices"] = clone(def["devices"])
		for _, kind := range []string{"people", "devices"} {
			for _, v := range arr(n[kind]) {
				m := obj(v)
				previous := find(old, kind, m["id"])
				ver := integer(m["version"])
				if x := integer(previous["version"]); x > ver {
					ver = x
				}
				merge(m, M{"version": ver + 1, "updatedAt": at, "updatedBy": op})
				if kind == "people" {
					m["status"] = "available"
				}
			}
		}
		for _, v := range arr(old["devices"]) {
			d := obj(v)
			if find(n, "devices", d["id"]) == nil {
				d = obj(clone(d))
				merge(d, M{"personId": "", "mode": "dashboard", "version": integer(d["version"]) + 1})
				add(n, "devices", d)
			}
		}
	}
	n["configs"] = clone(old["configs"])
	return n
}
func (e *engine) commands(ctx context.Context, requests A, batch bool) (reply M, err error) {
	started := time.Now()
	if len(requests) < 1 || len(requests) > 50 {
		return nil, problem{"Send between 1 and 50 station actions."}
	}
	seen := map[string]bool{}
	hashes := []string{}
	for _, v := range requests {
		req, ok := v.(map[string]any)
		if !ok {
			return nil, problem{"Invalid command."}
		}
		rid, op, action := text(req["requestId"]), strings.TrimSpace(text(req["operator"])), text(req["action"])
		_, argsOK := req["args"].(map[string]any)
		_, ridOK := req["requestId"].(string)
		_, actionOK := req["action"].(string)
		_, opOK := req["operator"].(string)
		if !ridOK || !actionOK || batch && !opOK || len(rid) < 8 || len(rid) > 100 || seen[rid] || op == "" || len([]rune(op)) > 100 || !argsOK || action == "" {
			return nil, problem{"Enter a valid operator, action, arguments and distinct request ID."}
		}
		if batch && (!queued[action] || req["queued"] != true) {
			return nil, problem{"Batches accept queued station actions only."}
		}
		seen[rid] = true
		hashes = append(hashes, digest(req))
	}
	e.writeMu.Lock()
	defer e.writeMu.Unlock()
	timings := M{"lock_seconds": time.Since(started).Seconds()}
	e.mu.RLock()
	dirty := e.dirty
	e.mu.RUnlock()
	if dirty {
		if err = e.reload(ctx); err != nil {
			return nil, err
		}
	}
	e.mu.RLock()
	before := e.state
	history := e.history
	remap := map[string]any{}
	for k, v := range e.remap {
		remap[k] = v
	}
	e.mu.RUnlock()
	sess := e.db.open()
	defer func() {
		if err != nil {
			sess.rollback()
			if !isProblem(err) {
				e.invalidate()
			}
		}
	}()
	phase := time.Now()
	placeholders := make([]string, len(requests))
	params := A{}
	for i, v := range requests {
		placeholders[i] = "?"
		params = append(params, obj(v)["requestId"])
	}
	rows, x := sess.group(ctx, sql("BEGIN IMMEDIATE"), sql("SELECT id,revision FROM tm_runs WHERE id=(SELECT value FROM meta WHERE key='current')"), statement{"SELECT r.id,r.digest,r.run_id,t.result FROM requests r LEFT JOIN tm_results t ON t.request_id=r.id WHERE r.id IN (" + strings.Join(placeholders, ",") + ")", params})
	if x != nil {
		return nil, x
	}
	timings["read_seconds"] = time.Since(phase).Seconds()
	if len(rows[1]) != 1 || !same(arr(rows[1][0])[0], before["runId"]) || !same(arr(rows[1][0])[1], before["revision"]) {
		e.invalidate()
		return nil, unavailable
	}
	prior := map[string]A{}
	for _, v := range rows[2] {
		r := arr(v)
		prior[text(r[0])] = r
	}
	phase = time.Now()
	work := obj(clone(nonEvents(before)))
	work["events"] = A{}
	results, newEvents := A{}, A{}
	writes := []statement{}
	var archive M
	for i, v := range requests {
		req := obj(v)
		rid, action, op := text(req["requestId"]), text(req["action"]), strings.TrimSpace(text(req["operator"]))
		if p, ok := prior[rid]; ok {
			if subtle.ConstantTimeCompare([]byte(text(p[1])), []byte(hashes[i])) != 1 {
				return nil, problem{"Request ID already used for a different action."}
			}
			var event any
			if p[3] != nil {
				event, x = parse([]byte(text(p[3])))
				if x != nil {
					return nil, unavailable
				}
			}
			results = append(results, M{"requestId": rid, "duplicate": true, "event": event})
			continue
		}
		if !same(req["runId"], work["runId"]) {
			return nil, problem{"The active run changed. Review held station entries."}
		}
		rev := req["revision"]
		if !isInt(rev) || number(rev) != float64(integer(rev)) || number(rev) < 0 || integer(rev) > integer(work["revision"]) {
			return nil, problem{"Invalid run revision."}
		}
		isQueued := req["queued"] == true && queued[action]
		if integer(rev) != integer(work["revision"]) && !staff[action] && action != "safetyReport" && !isQueued {
			intervening := append(append(A{}, history...), newEvents...)
			for _, vv := range intervening {
				event := obj(vv)
				if integer(event["revision"]) > integer(rev) && !staff[text(event["action"])] {
					return nil, problem{"Another station updated the run. The latest data has been loaded; review and retry."}
				}
			}
		}
		args := obj(clone(req["args"]))
		for _, key := range []string{"bag", "movement", "delivery"} {
			if replacement, ok := remap[text(args[key])]; ok {
				args[key] = replacement
			}
		}
		deliverySelection := action == "move" || action == "receive" || action == "routePickup" && text(args["source"]) != "labels"
		if isQueued && deliverySelection {
			if _, ok := args["expectedBagIds"].([]any); !ok {
				return nil, problem{"Queued delivery requires the exact selected bags."}
			}
		}
		if values, ok := args["expectedBagIds"].([]any); ok {
			for j, id := range values {
				if replacement, found := remap[text(id)]; found {
					values[j] = replacement
				}
			}
		}
		if isQueued && action == "fill" {
			if _, ok := args["mixBatch"].(string); !ok {
				return nil, problem{"Queued fill requires the recorded mixing batch."}
			}
		}
		old := auditBefore(work, action, args)
		at := now()
		if action == "workflowUpgrade" {
			work["events"] = history
		}
		r, applyErr := applyAction(work, action, args, op, at, e.pin, uuid)
		work["events"] = A{}
		if applyErr != nil {
			return nil, applyErr
		}
		work["revision"] = integer(work["revision"]) + 1
		if action == "newRun" {
			archive = work
			work = nextRun(archive, args, op, at)
			r["nextRunId"] = work["runId"]
		}
		eventState := work
		if archive != nil {
			eventState = archive
		}
		event := makeEvent(old, eventState, req, args, r, at, uuid())
		summary := eventSummary(event)
		if marker := map[string]string{"workflowUpgrade": "delivery_limits_v3", "routePlanUpgrade": "direct_sealing_v4", "rosterImport": "class_roster_v1"}[action]; marker != "" {
			writes = append(writes, sql("INSERT INTO meta(key,value) VALUES (?,?) ON CONFLICT(key) DO NOTHING", marker, "done"))
		}
		newEvents = append(newEvents, summary)
		writes = append(writes, auditWrite(text(old["runId"]), event), sql("INSERT INTO requests(id,digest,run_id) VALUES (?,?,?)", rid, hashes[i], old["runId"]), sql("INSERT INTO tm_results(request_id,result) VALUES (?,?)", rid, string(encode(summary))), sql("INSERT INTO tm_event_index(run_id,id,revision,summary) VALUES (?,?,?,?)", old["runId"], event["id"], event["revision"], string(encode(summary))))
		resultEvent := M{}
		for k, v := range event {
			if k != "before" && k != "after" {
				resultEvent[k] = v
			}
		}
		results = append(results, M{"requestId": rid, "duplicate": false, "event": resultEvent})
		indexPending(remap, summary)
	}
	timings["apply_seconds"] = time.Since(phase).Seconds()
	phase = time.Now()
	changed := len(newEvents) > 0
	if changed {
		saved := work
		archived := int64(0)
		if archive != nil {
			saved = archive
			archived = 1
		}
		writes = append(writes, entityWrites(before, saved)...)
		writes = append(writes, sql("UPDATE tm_runs SET revision=?,core=?,archived=? WHERE id=?", saved["revision"], string(encode(core(saved))), archived, saved["runId"]))
		if archive != nil {
			writes = append(writes, insertRun(work, 0))
			writes = append(writes, entityWrites(nil, work)...)
			writes = append(writes, sql("UPDATE meta SET value=? WHERE key='current'", work["runId"]))
		}
	}
	timings["encode_seconds"] = time.Since(phase).Seconds()
	phase = time.Now()
	if len(writes) > 0 {
		if _, x = sess.group(ctx, coalesce(writes)...); x != nil {
			return nil, x
		}
	}
	timings["write_seconds"] = time.Since(phase).Seconds()
	phase = time.Now()
	if x = sess.commit(ctx); x != nil {
		return nil, x
	}
	timings["commit_seconds"] = time.Since(phase).Seconds()
	phase = time.Now()
	if changed {
		delta := stateDelta(before, work, newEvents)
		if archive != nil {
			work["events"] = A{}
			delta = M{"reset": work}
		}
		e.mu.Lock()
		e.state = work
		e.history = append(append(A{}, history...), newEvents...)
		if archive != nil {
			e.history = A{}
			e.remap = map[string]any{}
		} else {
			e.remap = remap
		}
		e.serial++
		e.changes = append(e.changes, change{e.serial, text(work["runId"]), integer(work["revision"]), delta})
		if len(e.changes) > 128 {
			e.changes = e.changes[len(e.changes)-128:]
		}
		close(e.notify)
		e.notify = make(chan struct{})
		e.mu.Unlock()
		reply = M{"delta": delta, "results": results}
	} else {
		reply = M{"delta": M{"runId": before["runId"], "baseRevision": before["revision"], "revision": before["revision"], "core": core(before), "entities": M{}, "eventsAppend": A{}}, "results": results}
	}
	timings["publish_seconds"] = time.Since(phase).Seconds()
	timings["total_seconds"] = time.Since(started).Seconds()
	reply["timings"] = timings
	reply["databaseTrips"] = sess.Calls
	reply["databaseRequestBytes"] = sess.Bytes
	return reply, nil
}
func indexPending(target map[string]any, event M) {
	temp := engine{remap: target}
	temp.indexRemap(event)
}
func isProblem(err error) bool { var p problem; return errors.As(err, &p) }
func (e *engine) bootstrap(ctx context.Context) error {
	rows, err := e.db.query(ctx, sql("SELECT key,value FROM meta"))
	if err != nil {
		return err
	}
	meta := map[string]bool{}
	for _, v := range rows[0] {
		meta[text(arr(v)[0])] = true
	}
	for _, upgrade := range [][2]string{{"delivery_limits_v3", "workflowUpgrade"}, {"direct_sealing_v4", "routePlanUpgrade"}, {"class_roster_v1", "rosterImport"}} {
		if meta[upgrade[0]] {
			continue
		}
		if upgrade[1] == "rosterImport" && env("TRAILMIX_SEED_ROSTER", "1") == "0" {
			continue
		}
		if upgrade[1] != "rosterImport" && env("TRAILMIX_UPGRADE_WORKFLOW", "1") == "0" {
			continue
		}
		s := e.view()
		req := M{"action": upgrade[1], "args": M{}, "operator": "Workflow update", "requestId": "go-bootstrap-" + upgrade[0], "runId": s["runId"], "revision": s["revision"]}
		if _, err = e.commands(ctx, A{req}, false); err != nil {
			return err
		}

	}
	return nil
}
func (e *engine) hello(ctx context.Context, req M) (M, error) {
	did := text(req["deviceId"])
	if !validDevice(did) {
		return nil, problem{"Invalid device identifier."}
	}
	view := e.view()
	if find(view, "devices", did) == nil {
		r := M{"action": "deviceRegister", "args": M{"device": did}, "operator": "Device setup", "deviceId": did, "requestId": uuid(), "runId": view["runId"], "revision": view["revision"]}
		if _, err := e.commands(ctx, A{r}, false); err != nil {
			return nil, err
		}
	}
	e.touch(did, text(req["mode"]), text(req["operator"]))
	return e.view(), nil
}
func (e *engine) audit(ctx context.Context, run string) (M, error) {
	state, events, err := loadSnapshot(ctx, e.db, run, 2)
	if err != nil {
		return nil, err
	}
	state["events"] = events
	if run == "" {
		state["devices"] = e.devices(state)
	}
	return state, nil
}

// Snapshot only the bags an action may change, not every bag for every entry
// in a batch. Historical event lists never enter this working copy.
func auditBefore(s M, action string, args M) M {
	selected := A{}
	if id, ok := args["bag"]; ok {
		selected = append(selected, id)
	}
	if action == "move" || action == "receive" || action == "routePickup" && text(args["source"]) != "labels" {
		if expected, ok := args["expectedBagIds"]; ok {
			selected = arr(expected)
		} else {
			c := actionContext{s: s, a: args}
			bs := c.queue(text(args["source"]), args["station"])
			n := int(integer(args["qty"]))
			if n < 0 {
				n = 0
			}
			if n > len(bs) {
				n = len(bs)
			}
			selected = ids(bs[:n])
		}
	}
	if action == "routeDeliver" {
		selected = arr(find(s, "deliveries", args["delivery"])["remainingBagIds"])
	}
	if action == "reconcile" || action == "workflowUpgrade" || action == "routePlanUpgrade" {
		selected = ids(arr(s["bags"]))
	}
	return M{"runId": s["runId"], "people": clone(s["people"]), "bags": mapFromIDs(s, selected)}
}
