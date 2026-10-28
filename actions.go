package main

import (
	"crypto/subtle"
	"fmt"
	"math"
	"sort"
	"strings"
	"unicode/utf8"
)

type problem struct{ message string }

func (p problem) Error() string { return p.message }
func require(ok bool, msg string) {
	if !ok {
		panic(problem{msg})
	}
}

type actionContext struct {
	s   M
	a   M
	op  string
	at  int64
	pin string
	id  func() string
}

func applyAction(s M, action string, a M, op string, at int64, pin string, id func() string) (result M, err error) {
	defer func() {
		if v := recover(); v != nil {
			if p, ok := v.(problem); ok {
				err = p
			} else {
				panic(v)
			}
		}
	}()
	c := actionContext{s, a, op, at, pin, id}
	return c.run(action), nil
}
func (c actionContext) safety() {
	require(subtle.ConstantTimeCompare([]byte(text(c.a["safetyPin"])), []byte(c.pin)) == 1, "Safety Rover PIN required for this decision.")
}
func (c actionContext) note() string {
	v := strings.TrimSpace(text(c.a["note"]))
	require(v != "" && utf8.RuneCountInString(v) <= 4000, "Add a reason or note (up to 4,000 characters).")
	return v
}
func (c actionContext) qty() int {
	v := c.a["qty"]
	require(isInt(v) && number(v) == math.Trunc(number(v)) && number(v) >= 1 && number(v) <= 10000, "Enter a whole quantity from 1 to 10,000.")
	return int(number(v))
}
func (c actionContext) queue(status string, station any) A {
	bs := A{}
	for _, v := range arr(c.s["bags"]) {
		b := obj(v)
		if text(b["status"]) == status && (station == nil || same(b["station"], station)) {
			bs = append(bs, b)
		}
	}
	sort.SliceStable(bs, func(i, j int) bool {
		a, b := obj(bs[i]), obj(bs[j])
		x, y := number(a["updated"]), number(b["updated"])
		if status == "rework" {
			if a["holdAt"] != nil {
				x = number(a["holdAt"])
			}
			if b["holdAt"] != nil {
				y = number(b["holdAt"])
			}
		}
		if x != y {
			return x < y
		}
		return number(a["id"]) < number(b["id"])
	})
	return bs
}
func (c actionContext) bag(statuses string) M {
	b := find(c.s, "bags", c.a["bag"])
	require(b != nil && words(statuses)[text(b["status"])], "Bag is no longer in that queue. Refresh and retry.")
	return b
}
func (c actionContext) move(bs A, status string) {
	for _, v := range bs {
		b := obj(v)
		b["status"] = status
		b["updated"] = c.at
	}
}
func (c actionContext) take(status string) A {
	n := c.qty()
	bs := c.queue(status, c.a["station"])
	require(len(bs) >= n, "Quantity exceeds available bags.")
	bs = bs[:n]
	if expected, ok := c.a["expectedBagIds"]; ok {
		require(equal(expected, ids(bs)), "The selected bags changed. Review the held delivery before retrying.")
	}
	return bs
}
func result(kind string, bs A, detail string, extra M) M {
	m := M{"type": kind, "bags": ids(bs), "detail": detail}
	merge(m, extra)
	return m
}
func (c actionContext) shipment(source string, station any, bs A, movement M, legacy bool, who string, at int64) M {
	if who == "" {
		who = c.op
	}
	p := person(c.s, who)
	var pid any
	if p != nil {
		pid = p["id"]
	}
	var mid any
	qty := any(len(bs))
	if movement != nil {
		mid = movement["id"]
		qty = movement["qty"]
	}
	d := M{"id": c.id(), "source": source, "station": station, "bagIds": ids(bs), "remainingBagIds": ids(bs), "qty": qty, "movementId": mid, "status": "transit", "pickedAt": at, "pickedBy": who, "personId": pid, "receipts": A{}, "legacy": legacy}
	add(c.s, "deliveries", d)
	return d
}
func (c actionContext) receipt(bs A, movement any) A {
	bagIDs := ids(bs)
	changes := A{}
	for _, v := range arr(c.s["deliveries"]) {
		d := obj(v)
		if text(d["status"]) != "transit" {
			continue
		}
		received, remaining := A{}, A{}
		for _, id := range arr(d["remainingBagIds"]) {
			if has(bagIDs, id) {
				received = append(received, id)
			} else {
				remaining = append(remaining, id)
			}
		}
		labelMatch := movement != nil && same(d["movementId"], movement)
		if len(received) == 0 && !labelMatch {
			continue
		}
		before := clone(d)
		d["remainingBagIds"] = remaining
		qty := any(len(received))
		if labelMatch {
			qty = d["qty"]
		}
		add(d, "receipts", M{"at": c.at, "operator": c.op, "qty": qty, "bags": received})
		if labelMatch || len(remaining) == 0 {
			merge(d, M{"status": "delivered", "deliveredAt": c.at, "deliveredBy": c.op})
		}
		changes = append(changes, M{"before": before, "after": clone(d)})
	}
	return changes
}
func (c actionContext) mass() float64 {
	value := c.a["mass"]
	require(validNumber(value) && number(value) > 0 && number(value) <= 10000, "Enter a positive, finite net mass.")
	unit := text(c.a["unit"])
	require(unit == "oz" || unit == "g", "Select oz or g.")
	oz := number(value)
	if unit == "g" {
		oz /= 28.349523125
	}
	return oz
}
func (c actionContext) inSpec(oz float64) bool {
	limits := obj(c.s["limits"])
	return number(limits["min"])-1e-9 <= oz && oz <= number(limits["max"])+1e-9
}
func (c actionContext) measure(b M, kind string) (float64, bool) {
	oz := c.mass()
	pass := c.inSpec(oz)
	limits := obj(c.s["limits"])
	add(b, "measurements", M{"id": c.id(), "oz": oz, "entered": c.a["mass"], "unit": c.a["unit"], "at": c.at, "operator": c.op, "kind": kind, "minOz": limits["min"], "maxOz": limits["max"], "passed": pass})
	return oz, pass
}
func (c actionContext) newBag(station M, extra M) M {
	b := M{"id": c.s["next"], "station": station["id"], "mixBatch": station["batch"], "status": "filled", "created": c.at, "updated": c.at, "measurements": A{}, "firstFail": false, "sealFails": 0}
	merge(b, extra)
	c.s["next"] = integer(c.s["next"]) + 1
	add(c.s, "bags", b)
	return b
}
func (c actionContext) run(action string) M {
	s, a := c.s, c.a
	if truth(s["safetyHold"]) {
		require(staff[action] || words("workflowUpgrade routePlanUpgrade safetyRelease note safetyReport spotcheck")[action], "Production is on a safety hold. Ask the Safety Rover to release it.")
	}
	if r, ok := c.staffAction(action); ok {
		return r
	}
	switch action {
	case "routePickup":
		source := text(a["source"])
		station := a["station"]
		if source != "labels" && source != "filled" {
			station = nil
		}
		require(routeAllowed(person(s, c.op), source, station) && ((source != "labels" && source != "filled") || find(s, "stations", station) != nil), "This delivery leg and filling station are not assigned to you.")
		aa := obj(clone(a))
		aa["station"] = station
		next := c
		next.a = aa
		which := "move"
		if source == "labels" {
			which = "labelPickup"
		}
		r := next.run(which)
		r["type"] = "Route pickup"
		return r
	case "routeDeliver":
		d := find(s, "deliveries", a["delivery"])
		p := person(s, c.op)
		require(d != nil && text(d["status"]) == "transit", "This package is already delivered or unavailable.")
		owned := strings.EqualFold(text(d["pickedBy"]), c.op) || (p != nil && same(p["id"], d["personId"]))
		require(owned || (truth(d["legacy"]) && routeAllowed(p, text(d["source"]), d["station"])), "This package was picked up by a different operator.")
		if text(d["source"]) == "labels" {
			next := c
			next.a = M{"movement": d["movementId"]}
			r := next.run("receiveLabels")
			r["type"] = "Route delivered"
			return r
		}
		source := legs[text(d["source"])][3]
		bs := A{}
		for _, id := range arr(d["remainingBagIds"]) {
			b := find(s, "bags", id)
			require(b != nil && text(b["status"]) == source, "Package contents changed. Review this delivery before proceeding.")
			bs = append(bs, b)
		}
		require(len(bs) > 0, "Package contents changed. Review this delivery before proceeding.")
		dest := map[string]string{"inspectionTransit": "inspection", "reworkTransit": "rework", "returnTransit": "sealing", "sealTransit": "sealing", "finalTransit": "final"}[source]
		c.move(bs, dest)
		return result("Route delivered", bs, text(d["source"])+" delivered", M{"deliveryReceipts": c.receipt(bs, nil), "qty": len(bs)})
	case "label":
		n := c.qty()
		s["labels"] = integer(s["labels"]) + int64(n)
		return result("Labeled bags added", nil, "", M{"qty": n})
	case "labelPickup":
		n := c.qty()
		st := find(s, "stations", a["station"])
		require(st != nil && int64(n) <= integer(s["labels"]), "Choose a valid station and available quantity.")
		s["labels"] = integer(s["labels"]) - int64(n)
		m := M{"id": s["next"], "station": st["id"], "qty": n, "at": c.at, "operator": c.op, "received": false}
		s["next"] = integer(s["next"]) + 1
		add(s, "moves", m)
		d := c.shipment("labels", st["id"], nil, m, false, "", c.at)
		return result("Labeled bags picked up", nil, "To filling "+text(st["id"]), M{"qty": n, "movementId": m["id"], "deliveryIds": A{d["id"]}})
	case "receiveLabels":
		m := find(s, "moves", a["movement"])
		require(m != nil && !truth(m["received"]), "Pickup already received or not found.")
		st := find(s, "stations", m["station"])
		require(st != nil, "Filling station not found.")
		st["empty"] = integer(st["empty"]) + integer(m["qty"])
		merge(m, M{"received": true, "receivedAt": c.at, "receivedBy": c.op})
		return result("Empty bags received", nil, "Filling "+text(st["id"]), M{"qty": m["qty"], "movementId": m["id"], "deliveryReceipts": c.receipt(nil, m["id"])})
	case "assignBatch":
		st := find(s, "stations", a["station"])
		batch := strings.TrimSpace(text(a["batch"]))
		require(st != nil && batch != "" && utf8.RuneCountInString(batch) <= 100, "Enter a valid mixing batch ID, up to 100 characters.")
		previous := st["batch"]
		st["batch"] = batch
		return result("Mix batch assigned", nil, "Filling "+text(st["id"]), M{"previous": previous, "batch": batch})
	case "fill":
		st := find(s, "stations", a["station"])
		require(st != nil && number(st["empty"]) >= 1, "Receive labeled empty bags first.")
		require(text(st["batch"]) != "", "Assign the mixing batch ID before filling.")
		if batch, ok := a["mixBatch"]; ok {
			require(same(batch, st["batch"]), "The mixing batch changed. Review the held fill before retrying.")
		}
		st["empty"] = integer(st["empty"]) - 1
		b := c.newBag(st, M{})
		return result("Bag filled", A{b}, "Filling "+text(st["id"]), nil)
	case "move":
		routes := map[string]string{"filled": "inspectionTransit", "hold": "reworkTransit", "returnReady": "returnTransit", "ready": "sealTransit", "sealed": "finalTransit"}
		source := text(a["source"])
		dest, ok := routes[source]
		require(ok, "Invalid pickup route.")
		bs := c.take(source)
		if source == "returnReady" || source == "ready" {
			for _, v := range bs {
				b := obj(v)
				var latest M
				ms := arr(b["measurements"])
				for i := len(ms) - 1; i >= 0; i-- {
					m := obj(ms[i])
					if text(m["kind"]) == "rework" || (source == "ready" && text(m["kind"]) == "inspection") {
						latest = m
						break
					}
				}
				kind := "First inspection"
				if source == "returnReady" {
					kind = "Rework"
				}
				require(latest != nil && c.inSpec(number(latest["oz"])), kind+" must be in spec before delivery.")
			}
		}
		c.move(bs, dest)
		ds := A{}
		if source == "filled" {
			stations := map[int64]bool{}
			for _, b := range bs {
				stations[integer(obj(b)["station"])] = true
			}
			keys := []int64{}
			for id := range stations {
				keys = append(keys, id)
			}
			sort.Slice(keys, func(i, j int) bool { return keys[i] < keys[j] })
			for _, id := range keys {
				group := A{}
				for _, b := range bs {
					if integer(obj(b)["station"]) == id {
						group = append(group, b)
					}
				}
				ds = append(ds, c.shipment(source, id, group, nil, false, "", c.at)["id"])
			}
		} else {
			ds = append(ds, c.shipment(source, nil, bs, nil, false, "", c.at)["id"])
		}
		return result("Transport pickup", bs, source+" → "+dest, M{"source": source, "destination": dest, "deliveryIds": ds})
	case "receive":
		routes := map[string]string{"inspectionTransit": "inspection", "reworkTransit": "rework", "returnTransit": "sealing", "sealTransit": "sealing", "finalTransit": "final"}
		source := text(a["source"])
		dest, ok := routes[source]
		require(ok, "Invalid receipt route.")
		bs := c.take(source)
		c.move(bs, dest)
		return result("Transport received", bs, source+" → "+dest, M{"source": source, "destination": dest, "deliveryReceipts": c.receipt(bs, nil)})
	case "weigh", "clear":
		status := "inspection"
		kind := "inspection"
		if action == "clear" {
			status = "rework"
			kind = "rework"
		}
		b := c.bag(status)
		require(same(obj(c.queue(status, nil)[0])["id"], b["id"]), "Process the oldest bag first.")
		if action == "weigh" {
			_, ok := a["labelOK"].(bool)
			require(ok, "Record the label check.")
		}
		if action == "clear" {
			msg := fmt.Sprintf("Rework must be %.2f–%.2f oz net before delivery. Correct the mass and try again.", number(obj(s["limits"])["min"]), number(obj(s["limits"])["max"]))
			valid := validNumber(a["mass"]) && (text(a["unit"]) == "g" || text(a["unit"]) == "oz")
			oz := number(a["mass"])
			if text(a["unit"]) == "g" {
				oz /= 28.349523125
			}
			require(valid && c.inSpec(oz), msg)
		}
		oz, pass := c.measure(b, kind)
		if action == "clear" {
			c.move(A{b}, "returnReady")
			return result("Rework cleared for sealing", A{b}, fmt.Sprintf("%.4f oz net", oz), M{"massOz": oz})
		}
		inspections := 0
		for _, m := range arr(b["measurements"]) {
			if text(obj(m)["kind"]) == "inspection" {
				inspections++
			}
		}
		if inspections == 1 {
			b["firstFail"] = !pass
			b["firstLabelFail"] = !truth(a["labelOK"])
		}
		b["labelOK"] = a["labelOK"]
		if !truth(a["labelOK"]) {
			c.move(A{b}, "discarded")
			b["discardReason"] = "First inspection label failure"
			return result("Label failed · discarded", A{b}, text(b["discardReason"]), M{"massOz": oz})
		}
		if pass {
			c.move(A{b}, "ready")
			return result("Weight passed", A{b}, fmt.Sprintf("%.4f oz net", oz), M{"massOz": oz})
		}
		b["holdAt"] = c.at
		c.move(A{b}, "hold")
		detail := "Overweight → rework hold"
		if oz < number(obj(s["limits"])["min"]) {
			detail = "Underweight → rework hold"
		}
		return result("Weight failed", A{b}, detail, M{"massOz": oz})
	case "seal", "failSeal":
		b := c.bag("sealing")
		if action == "seal" {
			b["sealedAt"] = c.at
			c.move(A{b}, "sealed")
			return result("Seal passed", A{b}, "", nil)
		}
		note := c.note()
		b["sealFails"] = integer(b["sealFails"]) + 1
		c.move(A{b}, "sealFailed")
		return result("Seal failed", A{b}, note, nil)
	case "finish":
		b := c.bag("final")
		checks := obj(a["checks"])
		for _, k := range strings.Fields("seal label leak trappedProduct contamination") {
			require(checks[k] == true, "Confirm all five final inspection checks.")
		}
		b["completedAt"] = c.at
		c.move(A{b}, "done")
		return result("Final pass", A{b}, "Seal, label, leaks, trapped product and contamination checked", M{"checks": checks})
	case "finalFail":
		b := c.bag("final")
		note := c.note()
		c.move(A{b}, "finalHold")
		return result("Final check failed", A{b}, note, nil)
	case "disposition":
		c.safety()
		b := c.bag("qualityHold sealFailed finalHold")
		note := c.note()
		dest := text(a["destination"])
		switch dest {
		case "reseal":
			require(text(b["status"]) != "qualityHold" && a["bagLabelOK"] == true, "Reseal requires acceptable bag and label; weight holds cannot bypass inspection.")
			c.move(A{b}, "sealing")
		case "reinspect":
			c.move(A{b}, "inspection")
		case "discard":
			c.move(A{b}, "discarded")
			b["discardReason"] = note
		case "rebag":
			st := find(s, "stations", b["station"])
			require(st != nil && number(st["empty"]) >= 1, "Receive a labeled replacement bag at the original filling station first.")
			st["empty"] = integer(st["empty"]) - 1
			c.move(A{b}, "discarded")
			b["discardReason"] = "Rebagged: " + note
			replacement := c.newBag(st, M{"mixBatch": b["mixBatch"], "replaces": b["id"]})
			b["replacedBy"] = replacement["id"]
			return result("Safety disposition · rebag", A{b, replacement}, note, nil)
		default:
			require(false, "Choose a valid disposition.")
		}
		return result("Safety disposition · "+dest, A{b}, note, nil)
	case "spotcheck":
		c.safety()
		b := c.bag("final sealed done finalHold")
		note := c.note()
		oz, pass := c.measure(b, "spotcheck")
		if !pass {
			c.move(A{b}, "finalHold")
			delete(b, "completedAt")
		}
		return result("Weight spot check", A{b}, note, M{"massOz": oz, "passed": pass})
	case "safetyReport":
		note := c.note()
		s["safetyHold"] = true
		return result("Food safety issue reported · production held", nil, note, nil)
	case "safetyRelease":
		c.safety()
		note := c.note()
		s["safetyHold"] = false
		return result("Safety hold released", nil, note, nil)
	case "note":
		refs := a["references"]
		if refs == nil {
			refs = A{}
		}
		return result("Correction / production note", nil, c.note(), M{"references": refs})
	case "settings":
		name := strings.TrimSpace(text(a["name"]))
		require(name != "" && utf8.RuneCountInString(name) <= 100, "Enter a run name, up to 100 characters.")
		s["batch"] = name
		return result("Run renamed", nil, name, nil)
	case "station":
		n := len(arr(s["stations"]))
		require(n < 12, "Maximum 12 filling stations.")
		add(s, "stations", M{"id": n + 1, "empty": 0, "batch": ""})
		return result("Filling station added", nil, "", nil)
	case "newRun":
		c.safety()
		name := strings.TrimSpace(text(a["name"]))
		require(name != "" && utf8.RuneCountInString(name) <= 100, "Enter the new run name, up to 100 characters.")
		clear := number(s["labels"]) == 0
		for _, v := range arr(s["bags"]) {
			status := text(obj(v)["status"])
			clear = clear && (status == "done" || status == "discarded")
		}
		for _, v := range arr(s["stations"]) {
			clear = clear && number(obj(v)["empty"]) == 0
		}
		for _, v := range arr(s["moves"]) {
			clear = clear && truth(obj(v)["received"])
		}
		require(clear, "Account for all bags and empty inventory before archiving. Use a Safety Rover reconciliation.")
		choice := text(a["configId"])
		if choice == "" {
			choice = "current"
		}
		require(choice == "current" || find(s, "configs", choice) != nil, "Saved configuration not found.")
		return result("Run archived", nil, "Next run: "+name, M{"configuration": choice})
	case "reconcile":
		c.safety()
		note := c.note()
		bs := A{}
		for _, v := range arr(s["bags"]) {
			b := obj(v)
			if text(b["status"]) != "done" && text(b["status"]) != "discarded" {
				b["discardReason"] = "End-of-run reconciliation: " + note
				bs = append(bs, b)
			}
		}
		c.move(bs, "discarded")
		transit := A{}
		for _, m := range arr(s["moves"]) {
			if !truth(obj(m)["received"]) {
				transit = append(transit, clone(m))
			}
		}
		inventory := M{"labels": s["labels"], "stations": clone(s["stations"]), "inTransit": transit}
		for _, v := range arr(s["deliveries"]) {
			d := obj(v)
			if text(d["status"]) == "transit" {
				merge(d, M{"status": "cancelled", "cancelledAt": c.at, "cancelledBy": c.op})
			}
		}
		s["labels"] = 0
		for _, v := range arr(s["stations"]) {
			obj(v)["empty"] = 0
		}
		for _, v := range arr(s["moves"]) {
			m := obj(v)
			if !truth(m["received"]) {
				merge(m, M{"received": true, "reconciledAt": c.at, "reconciliation": note})
			}
		}
		return result("End-of-run reconciliation", bs, note, M{"remainingInventory": inventory})
	}
	panic(problem{"Unknown action."})
}
