package main

import (
	"encoding/json"
	"fmt"
	"os"
	"reflect"
	"testing"
)

func comparable(v any) any {
	var out any
	if err := json.Unmarshal(encode(v), &out); err != nil {
		panic(err)
	}
	return out
}
func TestPythonWorkflowParity(t *testing.T) {
	raw, err := os.ReadFile("testdata/python_parity.json")
	if err != nil {
		t.Fatal(err)
	}
	value, err := parse(raw)
	if err != nil {
		t.Fatal(err)
	}
	for i, v := range arr(value) {
		f := obj(v)
		t.Run(fmt.Sprintf("%03d_%s", i, text(f["action"])), func(t *testing.T) {
			state := obj(clone(f["before"]))
			generated := arr(f["generatedIDs"])
			n := 0
			id := func() string {
				if n >= len(generated) {
					t.Fatalf("unexpected ID generation")
					return ""
				}
				s := text(generated[n])
				n++
				return s
			}
			r, err := applyAction(state, text(f["action"]), obj(clone(f["args"])), text(f["operator"]), integer(f["when"]), text(f["pin"]), id)
			if f["error"] != nil {
				if err == nil {
					t.Fatalf("expected rejection: %s", f["error"])
				}
				return
			}
			if err != nil {
				t.Fatal(err)
			}
			if !reflect.DeepEqual(comparable(r), comparable(f["result"])) {
				t.Errorf("result differs\nGo: %s\nPython: %s", encode(r), encode(f["result"]))
			}
			if !reflect.DeepEqual(comparable(state), comparable(f["after"])) {
				t.Errorf("state differs\nGo: %s\nPython: %s", encode(state), encode(f["after"]))
			}
			if n != len(generated) {
				t.Errorf("generated %d IDs; expected %d", n, len(generated))
			}
		})
	}
}
func TestStrictJSON(t *testing.T) {
	for _, s := range []string{`{} {}`, `{}oops`, `[] true`} {
		if _, err := parse([]byte(s)); err == nil {
			t.Errorf("accepted invalid input %q", s)
		}
	}
}
func TestReworkBoundaries(t *testing.T) {
	for _, mass := range []float64{1.99, 2, 2.25, 2.26} {
		s := fresh(uuid, 1)
		s["bags"] = A{M{"id": 1, "station": 1, "status": "rework", "created": 1, "updated": 1, "holdAt": 1, "measurements": A{}}}
		_, err := applyAction(s, "clear", M{"bag": 1, "mass": mass, "unit": "oz"}, "Tester", 2, "pin", uuid)
		want := mass >= 2 && mass <= 2.25
		if (err == nil) != want {
			t.Errorf("mass %v err %v", mass, err)
		}
		if !want && len(arr(obj(arr(s["bags"])[0])["measurements"])) != 0 {
			t.Fatal("invalid rework added a reading")
		}
	}
}

func TestLegacyRequestDigests(t *testing.T) {
	raw, err := os.ReadFile("testdata/python_digests.json")
	if err != nil {
		t.Fatal(err)
	}
	v, err := parse(raw)
	if err != nil {
		t.Fatal(err)
	}
	for i, entry := range arr(v) {
		c := obj(entry)
		if actual := digest(obj(c["request"])); actual != text(c["expected"]) {
			t.Errorf("case %d digest differs: %s", i, pyJSON(c["request"]))
		}
	}
}
