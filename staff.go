package main

import (
	"fmt"
	"strings"
	"unicode"
	"unicode/utf8"
)

func (c actionContext) roles() map[string]bool {
	r := words("label inspection rework sealing final stocker firstMover finalMover mixing safety coordinator")
	for _, v := range arr(c.s["stations"]) {
		id := text(obj(v)["id"])
		r["fill:"+id] = true
		for source := range legs {
			r["delivery:"+source+":"+id] = true
		}
	}
	for source := range legs {
		r["delivery:"+source+":*"] = true
	}
	return r
}
func (c actionContext) assignments() A {
	values, ok := c.a["assignments"]
	if !ok {
		values = A{}
	}
	_, ok = values.([]any)
	require(ok, "Select valid station assignments.")
	roles := c.roles()
	out := A{}
	for _, v := range arr(values) {
		r, ok := v.(string)
		require(ok && roles[r], "Select valid station assignments.")
		if !has(out, r) {
			out = append(out, r)
		}
	}
	return out
}
func (c actionContext) touch(p M) {
	merge(p, M{"version": integer(p["version"]) + 1, "updatedAt": c.at, "updatedBy": c.op})
}
func validDevice(did string) bool {
	if len(did) < 8 || len(did) > 100 {
		return false
	}
	for _, r := range did {
		if !unicode.IsLetter(r) && !unicode.IsDigit(r) && r != '-' && r != '_' {
			return false
		}
	}
	return true
}
func (c actionContext) staffAction(action string) (M, bool) {
	s, a := c.s, c.a
	roles := c.roles()
	done := func(r M) (M, bool) { return r, true }
	switch action {
	case "deviceRegister":
		did := text(a["device"])
		require(validDevice(did), "Invalid device identifier.")
		d := find(s, "devices", did)
		if d == nil {
			d = M{"id": did, "name": "Device " + did[len(did)-6:], "personId": "", "mode": "", "version": 0, "createdAt": c.at, "updatedAt": c.at, "updatedBy": c.op}
			add(s, "devices", d)
		}
		return done(result("Device registered", nil, text(d["name"]), M{"deviceAfter": clone(d)}))
	case "deviceUpdate":
		d := find(s, "devices", a["device"])
		require(d != nil, "Device not found. Open the app and connect it first.")
		require(equal(a["expectedVersion"], d["version"]), "This device was updated elsewhere. Review its current assignment and retry.")
		name, pid, mode := strings.TrimSpace(text(a["name"])), text(a["person"]), text(a["mode"])
		p := find(s, "people", pid)
		require(name != "" && utf8.RuneCountInString(name) <= 80 && (pid == "" || (p != nil && !truth(p["deletedAt"]))) && (roles[mode] || words("dashboard deliveryDisplay delivery")[mode] || mode == ""), "Choose a valid device name, person and station/view.")
		before := clone(d)
		merge(d, M{"name": name, "personId": pid, "mode": mode})
		c.touch(d)
		extra := M{"deviceBefore": before, "deviceAfter": clone(d)}
		if p != nil && roles[mode] && !words("stocker firstMover finalMover")[mode] && !has(p["assignments"], mode) {
			extra["personBefore"] = clone(p)
			add(p, "assignments", mode)
			c.touch(p)
			extra["personAfter"] = clone(p)
		}
		return done(result("Device assignment updated", nil, name, extra))
	case "assignAtStation", "configSave":
		p := person(s, c.op)
		require(p != nil && (has(p["assignments"], "coordinator") || has(p["assignments"], "safety")), "Select a coordinator or Safety operator to manage station assignments and saved configurations.")
		if action == "configSave" {
			name := strings.TrimSpace(text(a["name"]))
			require(name != "" && utf8.RuneCountInString(name) <= 100, "Enter a configuration name up to 100 characters.")
			var config M
			for _, v := range arr(s["configs"]) {
				if strings.EqualFold(text(obj(v)["name"]), name) {
					config = obj(v)
				}
			}
			if config != nil {
				require(equal(a["expectedVersion"], config["version"]), "This saved configuration changed. Review it before overwriting.")
			} else {
				require(len(arr(s["configs"])) < 20, "Up to 20 named configurations can be saved.")
			}
			var before any
			if config != nil {
				before = clone(config)
			}
			stations, people := A{}, A{}
			for _, v := range arr(s["stations"]) {
				stations = append(stations, M{"id": obj(v)["id"]})
			}
			for _, v := range arr(s["people"]) {
				if !truth(obj(v)["deletedAt"]) {
					people = append(people, clone(v))
				}
			}
			definition := M{"stations": stations, "people": people, "devices": clone(s["devices"])}
			if config == nil {
				config = M{"id": c.id(), "name": name, "createdAt": c.at, "version": 0}
				add(s, "configs", config)
			} else {
				config["version"] = integer(config["version"]) + 1
			}
			merge(config, M{"name": name, "definition": definition, "updatedAt": c.at, "updatedBy": c.op})
			return done(result("Configuration saved", nil, name, M{"configBefore": before, "configAfter": clone(config)}))
		}
		role := text(a["stationRole"])
		valid := words("label inspection rework sealing final")[role] || strings.HasPrefix(role, "fill:") && roles[role] || strings.HasPrefix(role, "delivery:") && roles[role]
		require(valid, "Choose a production station.")
		p = find(s, "people", a["person"])
		require(p != nil && !truth(p["deletedAt"]), "Choose an active operator.")
		require(equal(a["expectedVersion"], p["version"]), "This operator changed elsewhere. Review the current assignment and retry.")
		changes := A{}
		targets := A{p}
		for _, v := range arr(s["people"]) {
			if !same(obj(v)["id"], p["id"]) {
				targets = append(targets, v)
			}
		}
		for _, v := range targets {
			who := obj(v)
			target := same(who["id"], p["id"])
			if !target && !(a["replace"] == true && has(who["assignments"], role)) {
				continue
			}
			before := clone(who)
			if target {
				if !has(who["assignments"], role) {
					add(who, "assignments", role)
				}
				who["status"] = "working"
			} else {
				rs := A{}
				for _, r := range arr(who["assignments"]) {
					if text(r) != role {
						rs = append(rs, r)
					}
				}
				who["assignments"] = rs
			}
			c.touch(who)
			changes = append(changes, M{"before": before, "after": clone(who)})
		}
		devices := A{}
		if truth(a["device"]) {
			d := find(s, "devices", a["device"])
			require(d != nil, "Device not found. Open the app on it first.")
			before := clone(d)
			mode := role
			if strings.HasPrefix(role, "delivery:") {
				mode = "delivery"
			}
			merge(d, M{"personId": p["id"], "mode": mode})
			c.touch(d)
			devices = append(devices, M{"before": before, "after": clone(d)})
		}
		return done(result("Station operator assigned", nil, text(p["name"])+" → "+role, M{"peopleUpdated": changes, "devicesUpdated": devices}))
	case "rosterImport":
		added, updated := A{}, A{}
		for _, name := range roster {
			matches := A{}
			for _, v := range arr(s["people"]) {
				fields := strings.Fields(text(obj(v)["name"]))
				if len(fields) > 0 && strings.EqualFold(fields[0], name) {
					matches = append(matches, v)
				}
			}
			require(len(matches) <= 1, "Multiple people match "+name+". Rename the duplicates before importing.")
			rs := A{}
			if words("lucas blake charlie")[strings.ToLower(name)] {
				rs = append(rs, "safety")
			}
			if len(matches) > 0 {
				p := obj(matches[0])
				if truth(p["deletedAt"]) {
					continue
				}
				before := clone(p)
				p["assignments"] = rs
				c.touch(p)
				updated = append(updated, M{"before": before, "after": clone(p)})
			} else {
				p := M{"id": c.id(), "name": name, "assignments": rs, "status": "available", "createdAt": c.at, "updatedAt": c.at, "updatedBy": c.op, "version": 0}
				add(s, "people", p)
				added = append(added, clone(p))
			}
		}
		return done(result("Class roster prepared", nil, "Lucas, Blake, Charlie: Safety; other class members unassigned", M{"qty": len(added), "peopleAdded": added, "peopleUpdated": updated}))
	case "personDelete", "personRestore":
		p := find(s, "people", a["person"])
		require(p != nil, "Person not found.")
		before := clone(p)
		devices := A{}
		kind := "Operator restored"
		if action == "personDelete" {
			require(!truth(p["deletedAt"]), "Operator is already deleted.")
			merge(p, M{"deletedAt": c.at, "deletedBy": c.op, "status": "off"})
			kind = "Operator deleted"
			for _, v := range arr(s["devices"]) {
				d := obj(v)
				if same(d["personId"], p["id"]) {
					prev := clone(d)
					d["personId"] = ""
					c.touch(d)
					devices = append(devices, M{"before": prev, "after": clone(d)})
				}
			}
		} else {
			require(truth(p["deletedAt"]), "Operator is already active.")
			delete(p, "deletedAt")
			delete(p, "deletedBy")
			p["status"] = "available"
		}
		c.touch(p)
		return done(result(kind, nil, text(p["name"]), M{"personBefore": before, "personAfter": clone(p), "devicesUpdated": devices}))
	case "personAdd", "personAddMany":
		names := arr(a["names"])
		if action == "personAdd" {
			names = A{a["name"]}
		}
		require(len(names) >= 1 && len(names) <= 100, "Enter between 1 and 100 operator names.")
		known := map[string]bool{}
		for _, v := range arr(s["people"]) {
			known[strings.ToLower(text(obj(v)["name"]))] = true
		}
		clean := []string{}
		for _, v := range names {
			name, ok := v.(string)
			name = strings.TrimSpace(name)
			require(ok && name != "" && utf8.RuneCountInString(name) <= 100, "Enter a person name, up to 100 characters.")
			key := strings.ToLower(name)
			require(!known[key], "That name is already in the roster. Use a distinct name for each person.")
			known[key] = true
			clean = append(clean, name)
		}
		rs := c.assignments()
		people := A{}
		for _, name := range clean {
			p := M{"id": c.id(), "name": name, "assignments": clone(rs), "status": "available", "createdAt": c.at, "updatedAt": c.at, "updatedBy": c.op, "version": 0}
			add(s, "people", p)
			people = append(people, clone(p))
		}
		if action == "personAdd" {
			return done(result("Operator added", nil, clean[0], M{"personAfter": people[0]}))
		}
		return done(result("Operators prepared", nil, strings.Join(clean, ", "), M{"qty": len(people), "peopleAfter": people}))
	case "personUpdate":
		p := find(s, "people", a["person"])
		require(p != nil, "Person not found.")
		if expected, ok := a["expectedVersion"]; ok {
			require(equal(expected, p["version"]), "This assignment was changed by someone else. Review the latest assignments and retry.")
		}
		require(!truth(p["deletedAt"]), "Restore this operator before changing assignments.")
		name := text(p["name"])
		if v, ok := a["name"]; ok {
			name = strings.TrimSpace(text(v))
		}
		valid := name != "" && utf8.RuneCountInString(name) <= 100
		for _, v := range arr(s["people"]) {
			other := obj(v)
			if !same(p["id"], other["id"]) && strings.EqualFold(text(other["name"]), name) {
				valid = false
			}
		}
		require(valid, "Enter a distinct operator name up to 100 characters.")
		status := text(a["status"])
		require(words("available working break off")[status], "Select a valid operator status.")
		rs := c.assignments()
		before := clone(p)
		merge(p, M{"name": name, "assignments": rs, "status": status})
		c.touch(p)
		return done(result("Operator assignments updated", nil, name, M{"personBefore": before, "personAfter": clone(p)}))
	case "messagePost":
		message := strings.TrimSpace(text(a["text"]))
		targetType, target := text(a["targetType"]), text(a["target"])
		require(message != "" && utf8.RuneCountInString(message) <= 1000, "Enter a message, up to 1,000 characters.")
		require(words("person station delivery all")[targetType], "Choose a valid message audience.")
		require(targetType != "person" || find(s, "people", target) != nil, "Message recipient not found.")
		require(targetType != "station" || roles[target], "Message station not found.")
		priority := text(a["priority"])
		if priority == "" {
			priority = "normal"
		}
		require(priority == "normal" || priority == "urgent", "Choose normal or urgent priority.")
		if targetType != "person" && targetType != "station" {
			target = ""
		}
		m := M{"id": c.id(), "text": message, "targetType": targetType, "target": target, "priority": priority, "createdAt": c.at, "createdBy": c.op, "acknowledgements": A{}, "resolvedAt": nil, "resolvedBy": nil}
		add(s, "messages", m)
		return done(result("Operator message posted", nil, message, M{"messageAfter": clone(m)}))
	case "messageAcknowledge", "messageResolve":
		m := find(s, "messages", a["message"])
		require(m != nil && !truth(m["resolvedAt"]), "Message is already resolved or not found.")
		before := clone(m)
		kind := "Operator message resolved"
		if action == "messageResolve" {
			merge(m, M{"resolvedAt": c.at, "resolvedBy": c.op})
		} else {
			p := person(s, c.op)
			require(p != nil, "Select your roster name before acknowledging a message.")
			tt, target := text(m["targetType"]), m["target"]
			eligible := tt == "all" || tt == "person" && same(target, p["id"]) || tt == "station" && has(p["assignments"], target)
			for _, r := range arr(p["assignments"]) {
				if tt == "delivery" && (words("stocker firstMover finalMover")[text(r)] || strings.HasPrefix(text(r), "delivery:")) {
					eligible = true
				}
			}
			require(eligible, "This message is addressed to a different operator or assignment.")
			for _, ack := range arr(m["acknowledgements"]) {
				require(!same(obj(ack)["personId"], p["id"]), "You already acknowledged this message.")
			}
			add(m, "acknowledgements", M{"personId": p["id"], "operator": c.op, "at": c.at})
			kind = "Operator message acknowledged"
		}
		return done(result(kind, nil, text(m["text"]), M{"messageBefore": before, "messageAfter": clone(m)}))
	case "routePlanUpgrade":
		changes, affected := A{}, A{}
		for _, v := range arr(s["bags"]) {
			b := obj(v)
			ms := arr(b["measurements"])
			if text(b["status"]) == "inspection" && len(ms) > 0 {
				last := obj(ms[len(ms)-1])
				if text(last["kind"]) == "rework" && c.inSpec(number(last["oz"])) {
					merge(b, M{"status": "ready", "updated": c.at})
					affected = append(affected, b)
				}
			}
		}
		for _, v := range arr(s["people"]) {
			p := obj(v)
			before := clone(p)
			rs := A{}
			for _, r := range arr(p["assignments"]) {
				role := text(r)
				parts := strings.Split(role, ":")
				if len(parts) == 3 && parts[0] == "delivery" && parts[1] != "labels" && parts[1] != "filled" {
					role = "delivery:" + parts[1] + ":*"
				}
				if !has(rs, role) {
					rs = append(rs, role)
				}
			}
			p["assignments"] = rs
			if !equal(before, p) {
				c.touch(p)
				changes = append(changes, M{"before": before, "after": clone(p)})
			}
		}
		for _, v := range arr(s["deliveries"]) {
			d := obj(v)
			if text(d["status"]) == "transit" && text(d["source"]) != "labels" && text(d["source"]) != "filled" {
				d["station"] = nil
			}
		}
		return done(result("Rework route updated", affected, "Cleared rework goes directly to Sealing; downstream legs are shared", M{"peopleUpdated": changes}))
	case "workflowUpgrade":
		return done(c.workflowUpgrade())
	}
	return nil, false
}
func (c actionContext) workflowUpgrade() M {
	s := c.s
	old := clone(s["limits"])
	affected := A{}
	oldLimits := obj(s["limits"])
	for _, v := range arr(s["bags"]) {
		b := obj(v)
		var inspection, rework M
		for _, x := range arr(b["measurements"]) {
			m := obj(x)
			if m["minOz"] == nil {
				m["minOz"] = oldLimits["min"]
			}
			if m["maxOz"] == nil {
				m["maxOz"] = oldLimits["max"]
			}
			if m["passed"] == nil {
				m["passed"] = number(m["minOz"])-1e-9 <= number(m["oz"]) && number(m["oz"]) <= number(m["maxOz"])+1e-9
			}
			if text(m["kind"]) == "inspection" {
				inspection = m
			}
			if text(m["kind"]) == "rework" {
				rework = m
			}
		}
		if text(b["status"]) == "ready" && (inspection == nil || number(inspection["oz"]) < 2-1e-9 || number(inspection["oz"]) > 2.25+1e-9) {
			merge(b, M{"status": "hold", "holdAt": c.at, "updated": c.at})
			affected = append(affected, b)
		}
		if text(b["status"]) == "returnReady" && (rework == nil || number(rework["oz"]) < 2-1e-9 || number(rework["oz"]) > 2.25+1e-9) {
			merge(b, M{"status": "rework", "updated": c.at})
			affected = append(affected, b)
		}
	}
	s["limits"] = M{"min": 2.0, "max": 2.25, "confirmed": true}
	tracked := A{}
	trackedMoves := A{}
	for _, v := range arr(s["deliveries"]) {
		d := obj(v)
		if text(d["status"]) == "transit" {
			tracked = append(tracked, arr(d["remainingBagIds"])...)
			trackedMoves = append(trackedMoves, d["movementId"])
		}
	}
	keys := []string{"filled", "hold", "returnReady", "ready", "sealed"}
	for _, source := range keys {
		groups := map[string]A{}
		events := map[string]M{}
		for _, v := range arr(s["bags"]) {
			b := obj(v)
			if text(b["status"]) != legs[source][3] || has(tracked, b["id"]) {
				continue
			}
			var event M
			es := arr(s["events"])
			for i := len(es) - 1; i >= 0; i-- {
				e := obj(es[i])
				if text(e["action"]) == "move" && has(e["bags"], b["id"]) {
					event = e
					break
				}
			}
			key := fmt.Sprint(b["station"], "/", event["id"])
			groups[key] = append(groups[key], b)
			events[key] = event
		}
		for key, bs := range groups {
			e := events[key]
			op, at := "Previous pickup", c.at
			if e != nil {
				op = text(e["operator"])
				at = integer(e["at"])
			}
			c.shipment(source, obj(bs[0])["station"], bs, nil, true, op, at)
		}
	}
	for _, v := range arr(s["moves"]) {
		m := obj(v)
		if !truth(m["received"]) && !has(trackedMoves, m["id"]) {
			c.shipment("labels", m["station"], nil, m, true, text(m["operator"]), integer(m["at"]))
		}
	}
	return result("Delivery routes and weight limits updated", affected, "2.00–2.25 oz net", M{"previousLimits": old, "limits": clone(s["limits"]), "deliveries": clone(s["deliveries"])})
}
