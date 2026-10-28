package main

import (
	"bytes"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"math"
	"sort"
	"strconv"
	"strings"
	"time"
	"unicode/utf16"
)

type M = map[string]any
type A = []any

func obj(v any) M {
	if m, ok := v.(map[string]any); ok {
		return m
	}
	return M{}
}
func arr(v any) A {
	if a, ok := v.([]any); ok {
		return a
	}
	return A{}
}
func text(v any) string {
	if v == nil {
		return ""
	}
	switch s := v.(type) {
	case string:
		return s
	case json.Number:
		return string(s)
	case int:
		return strconv.Itoa(s)
	case int64:
		return strconv.FormatInt(s, 10)
	}
	return fmt.Sprint(v)
}
func number(v any) float64 {
	switch n := v.(type) {
	case json.Number:
		f, _ := n.Float64()
		return f
	case float64:
		return n
	case int:
		return float64(n)
	case int64:
		return float64(n)
	}
	return 0
}
func integer(v any) int64 { return int64(number(v)) }
func truth(v any) bool {
	if v == nil {
		return false
	}
	switch x := v.(type) {
	case bool:
		return x
	case string:
		return x != ""
	case json.Number, float64, int, int64:
		return number(x) != 0
	case []any:
		return len(x) > 0
	case map[string]any:
		return len(x) > 0
	}
	return true
}
func encode(v any) []byte {
	b, err := json.Marshal(v)
	if err != nil {
		panic(err)
	}
	return b
}
func parse(b []byte) (any, error) {
	d := json.NewDecoder(bytes.NewReader(b))
	d.UseNumber()
	var v any
	err := d.Decode(&v)
	if err == nil {
		var extra any
		if err := d.Decode(&extra); err != io.EOF {
			return nil, fmt.Errorf("invalid trailing JSON")
		}
	}
	return v, err
}
func clone(v any) any {
	switch x := v.(type) {
	case map[string]any:
		m := M{}
		for k, v := range x {
			m[k] = clone(v)
		}
		return m
	case []any:
		a := make(A, len(x))
		for i, v := range x {
			a[i] = clone(v)
		}
		return a
	default:
		return v
	}
}

// Compare JSON values without serializing every unchanged bag twice for SQL
// and again for sync. Keep the exact encoded semantics for numeric spellings,
// null versus empty containers and mixed Go/decoded JSON number types.
func equal(a, b any) bool {
	switch x := a.(type) {
	case map[string]any:
		if y, ok := b.(map[string]any); ok && x != nil && y != nil {
			if len(x) != len(y) {
				return false
			}
			for k, v := range x {
				other, exists := y[k]
				if !exists || !equal(v, other) {
					return false
				}
			}
			return true
		}
	case []any:
		if y, ok := b.([]any); ok && x != nil && y != nil {
			if len(x) != len(y) {
				return false
			}
			for i, v := range x {
				if !equal(v, y[i]) {
					return false
				}
			}
			return true
		}
	case string:
		y, ok := b.(string)
		return ok && x == y
	case bool:
		y, ok := b.(bool)
		return ok && x == y
	case json.Number:
		if y, ok := b.(json.Number); ok {
			return x == y
		}
	case int:
		if y, ok := b.(int); ok {
			return x == y
		}
	case int64:
		if y, ok := b.(int64); ok {
			return x == y
		}
	case float64:
		if y, ok := b.(float64); ok {
			return math.Float64bits(x) == math.Float64bits(y)
		}
	}
	return bytes.Equal(encode(a), encode(b))
}
func same(a, b any) bool { return text(a) == text(b) }
func has(a any, v any) bool {
	for _, x := range arr(a) {
		if same(x, v) {
			return true
		}
	}
	return false
}
func idSet(values any) map[string]bool {
	set := make(map[string]bool, len(arr(values)))
	for _, id := range arr(values) {
		set[text(id)] = true
	}
	return set
}
func entityIndex(s M, kind string) map[string]M {
	index := make(map[string]M, len(arr(s[kind])))
	for _, v := range arr(s[kind]) {
		m := obj(v)
		if _, exists := index[text(m["id"])]; !exists {
			index[text(m["id"])] = m
		}
	}
	return index
}
func find(s M, kind string, id any) M {
	for _, v := range arr(s[kind]) {
		m := obj(v)
		if same(m["id"], id) {
			return m
		}
	}
	return nil
}
func add(s M, key string, v any) { s[key] = append(arr(s[key]), v) }
func merge(a M, b M) {
	for k, v := range b {
		a[k] = v
	}
}
func ids(bs A) A {
	a := A{}
	for _, b := range bs {
		a = append(a, obj(b)["id"])
	}
	return a
}
func now() int64 { return time.Now().UnixMilli() }
func uuid() string {
	b := make([]byte, 16)
	if _, e := rand.Read(b); e != nil {
		panic(e)
	}
	b[6] = (b[6] & 15) | 64
	b[8] = (b[8] & 63) | 128
	return fmt.Sprintf("%x-%x-%x-%x-%x", b[:4], b[4:6], b[6:8], b[8:10], b[10:])
}
func nonEvents(s M) M {
	m := M{}
	for k, v := range s {
		if k != "events" {
			m[k] = v
		}
	}
	return m
}

var entityKinds = []string{"stations", "bags", "moves", "people", "messages", "devices", "deliveries", "configs"}

func core(s M) M {
	m := M{}
	for k, v := range s {
		if k == "events" {
			continue
		}
		found := false
		for _, kind := range entityKinds {
			if k == kind {
				found = true
			}
		}
		if !found {
			m[k] = v
		}
	}
	return m
}
func fresh(id func() string, at int64) M {
	return M{"version": 2, "revision": 0, "runId": id(), "batch": "Trail mix · Run 01", "started": at, "demo": false, "limits": M{"min": 2.0, "max": 2.25, "confirmed": true}, "stations": A{M{"id": 1, "empty": 0, "batch": ""}, M{"id": 2, "empty": 0, "batch": ""}, M{"id": 3, "empty": 0, "batch": ""}}, "labels": 0, "moves": A{}, "bags": A{}, "events": A{}, "next": 1, "safetyHold": false, "people": A{}, "messages": A{}, "devices": A{}, "deliveries": A{}, "configs": A{}}
}

var queued = words("routePickup routeDeliver label labelPickup receiveLabels fill move receive weigh clear seal failSeal finish finalFail")
var staff = words("assignAtStation configSave personDelete personRestore rosterImport personAddMany personAdd personUpdate messagePost messageAcknowledge messageResolve deviceRegister deviceUpdate")

func words(s string) map[string]bool {
	r := map[string]bool{}
	for _, v := range strings.Fields(s) {
		r[v] = true
	}
	return r
}

var legs = map[string][4]string{"labels": {"Labeling", "Filling", "stocker", ""}, "filled": {"Filling", "First inspection", "firstMover", "inspectionTransit"}, "hold": {"First inspection", "Rework", "finalMover", "reworkTransit"}, "returnReady": {"Rework", "Sealing", "finalMover", "returnTransit"}, "ready": {"First inspection", "Sealing", "finalMover", "sealTransit"}, "sealed": {"Sealing", "Final inspection", "finalMover", "finalTransit"}}
var roster = strings.Split("Truus Leslie Blake David Brian Aiden Megan Fabio Katelyn Elias Halen Dylan Luke Hailey Marie Keira Natalie Charlie Beaton Veena Nico Aum Alexander Martin Logan Lucas Vito Sarah", " ")

func person(s M, name string) M {
	for _, p := range arr(s["people"]) {
		m := obj(p)
		if !truth(m["deletedAt"]) && strings.EqualFold(text(m["name"]), name) {
			return m
		}
	}
	return nil
}
func routeAllowed(p M, source string, station any) bool {
	leg, ok := legs[source]
	if !ok || p == nil || truth(p["deletedAt"]) {
		return false
	}
	if has(p["assignments"], leg[2]) {
		return true
	}
	for _, r := range arr(p["assignments"]) {
		x := text(r)
		if source != "labels" && source != "filled" {
			if strings.HasPrefix(x, "delivery:"+source+":") {
				return true
			}
		} else if x == "delivery:"+source+":*" || x == "delivery:"+source+":"+text(station) {
			return true
		}
	}
	return false
}
func eventSummary(e M) M {
	m := M{}
	for _, k := range strings.Fields("id at operator requestId type action qty bags detail revision runId personId assignments deviceId deviceName recordedAt recordedStation") {
		if v, ok := e[k]; ok {
			m[k] = v
		}
	}
	m["data"] = M{}
	for k, v := range obj(e["data"]) {
		if k != "expectedBagIds" {
			obj(m["data"])[k] = v
		}
	}
	c := obj(e["changes"])
	m["changes"] = M{}
	for _, k := range strings.Fields("movementId deliveryIds") {
		if v, ok := c[k]; ok {
			obj(m["changes"])[k] = v
		}
	}
	return m
}

// Python's original digest must remain valid for browser outbox entries saved
// before migration. Canonical spacing, Unicode escapes and float formatting
// deliberately match json.dumps(sort_keys=True), including surrogate pairs.
func pyString(s string) string {
	var b strings.Builder
	b.WriteByte('"')
	for _, r := range s {
		switch r {
		case '"', '\\':
			b.WriteByte('\\')
			b.WriteRune(r)
		case '\b':
			b.WriteString(`\b`)
		case '\f':
			b.WriteString(`\f`)
		case '\n':
			b.WriteString(`\n`)
		case '\r':
			b.WriteString(`\r`)
		case '\t':
			b.WriteString(`\t`)
		default:
			if r < 32 || r > 126 {
				if r > 65535 {
					a, c := utf16.EncodeRune(r)
					fmt.Fprintf(&b, `\u%04x\u%04x`, a, c)
				} else {
					fmt.Fprintf(&b, `\u%04x`, r)
				}
			} else {
				b.WriteRune(r)
			}
		}
	}
	b.WriteByte('"')
	return b.String()
}
func pyNumber(n json.Number) string {
	s := string(n)
	if !strings.ContainsAny(s, ".eE") {
		return s
	}
	f, _ := n.Float64()
	s = strconv.FormatFloat(f, 'g', -1, 64)
	if strings.Contains(s, "e") {
		parts := strings.Split(s, "e")
		exp, _ := strconv.Atoi(parts[1])
		if exp >= -4 && exp < 16 {
			s = strconv.FormatFloat(f, 'f', -1, 64)
		} else {
			s = parts[0] + fmt.Sprintf("e%+03d", exp)
		}
	}
	if !strings.ContainsAny(s, ".e") {
		s += ".0"
	}
	return s
}
func pyJSON(v any) string {
	switch x := v.(type) {
	case nil:
		return "null"
	case bool:
		if x {
			return "true"
		}
		return "false"
	case string:
		return pyString(x)
	case json.Number:
		return pyNumber(x)
	case map[string]any:
		keys := make([]string, 0, len(x))
		for k := range x {
			keys = append(keys, k)
		}
		sort.Strings(keys)
		items := []string{}
		for _, k := range keys {
			items = append(items, pyString(k)+": "+pyJSON(x[k]))
		}
		return "{" + strings.Join(items, ", ") + "}"
	case []any:
		items := []string{}
		for _, v := range x {
			items = append(items, pyJSON(v))
		}
		return "[" + strings.Join(items, ", ") + "]"
	default:
		return string(encode(v))
	}
}
func digest(req M) string { v := sha256.Sum256([]byte(pyJSON(req))); return hex.EncodeToString(v[:]) }
func validNumber(v any) bool {
	switch v.(type) {
	case json.Number, float64, int, int64:
		return !math.IsNaN(number(v)) && !math.IsInf(number(v), 0)
	}
	return false
}

func isInt(v any) bool {
	switch n := v.(type) {
	case json.Number:
		return !strings.ContainsAny(string(n), ".eE")
	case int, int64:
		return true
	}
	return false
}
