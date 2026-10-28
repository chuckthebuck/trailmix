package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"math"
	"testing"
)

func movementFixture(n int) M {
	s := fresh(uuid, 1)
	for i := 1; i <= n; i++ {
		add(s, "bags", M{"id": json.Number(fmt.Sprint(i)), "station": json.Number("1"), "mixBatch": "STRESS", "status": "filled", "created": json.Number("1"), "updated": json.Number("1"), "measurements": A{}, "firstFail": false, "sealFails": json.Number("0")})
	}
	s["next"] = n + 1
	return s
}

// Include validation, before/after audit snapshots and both persistence/sync
// comparisons. This is the CPU work done while other devices wait to save.
func BenchmarkBulkMovement(b *testing.B) {
	for _, qty := range []int{16, 600, 2000} {
		b.Run(fmt.Sprint(qty), func(b *testing.B) {
			before := movementFixture(2000)
			selected := ids(arr(before["bags"])[:qty])
			args := M{"source": "filled", "qty": qty, "expectedBagIds": selected}
			b.ReportAllocs()
			for i := 0; i < b.N; i++ {
				work := obj(clone(before))
				old := auditBefore(work, "move", args)
				r, err := applyAction(work, "move", args, "Tester", 2, "pin", uuid)
				if err != nil {
					b.Fatal(err)
				}
				event := makeEvent(old, work, M{"action": "move", "operator": "Tester"}, args, r, 2, uuid())
				_ = auditWrite(text(before["runId"]), event)
				_ = entityWrites(before, work)
				_ = stateDelta(before, work, A{eventSummary(event)})
			}
		})
	}
}

func TestJSONEqualityMatchesEncoding(t *testing.T) {
	values := A{nil, M(nil), A(nil), M{}, A{}, true, false, "", "1", 1, int64(1), float64(1), json.Number("1"), json.Number("1.0"), json.Number("1e0"), float64(0), math.Copysign(0, -1), M{"a": 1, "b": A{true, nil}}, M{"b": A{true, nil}, "a": json.Number("1")}, M{"missing": nil}, A{1, "a"}, A{json.Number("1"), "a"}}
	for _, a := range values {
		for _, c := range values {
			want := bytes.Equal(encode(a), encode(c))
			if equal(a, c) != want {
				t.Fatalf("equality differs for %#v and %#v", a, c)
			}
		}
	}
}
