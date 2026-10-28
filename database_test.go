package main

import (
	"context"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestDatabaseURLValidation(t *testing.T) {
	for _, url := range []string{"http://database.test", "https://user:secret@database.test", "https://database.test?token=secret", "https://database.test/other"} {
		if _, err := newDatabase(url, "token", false); err == nil {
			t.Errorf("accepted %s", url)
		}
	}
	if _, err := newDatabase("libsql://database.test", "token", false); err != nil {
		t.Fatal(err)
	}
}
func TestNoCredentialForwardingToForeignRoutingURL(t *testing.T) {
	calls := 0
	gateway := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		_, _ = io.WriteString(w, `{"baton":"test","base_url":"https://foreign.invalid/v2/pipeline","results":[{"type":"ok","response":{"result":{"rows":[]}}}]}`)
	}))
	defer gateway.Close()
	db, err := newDatabase(gateway.URL, "token", true)
	if err != nil {
		t.Fatal(err)
	}
	s := db.open()
	if _, err = s.group(context.Background(), sql("BEGIN")); err == nil {
		t.Fatal("accepted untrusted routing host")
	}
	if _, err = s.group(context.Background(), sql("SELECT 1")); err == nil {
		t.Fatal("reused broken connection")
	}
	if calls != 1 {
		t.Fatal("sent additional requests after routing failure")
	}
}
func TestCoalescedInsertOrderAndParameterBounds(t *testing.T) {
	input := []statement{}
	for i := 0; i < 250; i++ {
		input = append(input, sql("INSERT INTO tm_entities(run_id,kind,id,position,body) VALUES (?,?,?,?,?) ON CONFLICT(run_id,kind,id) DO UPDATE SET body=excluded.body", "run", "bags", i, i, "{}"))
	}
	out := coalesce(input)
	if len(out) != 3 {
		t.Fatalf("wanted 3 bounded inserts, got %d", len(out))
	}
	for _, st := range out {
		if len(st.Args) > 500 {
			t.Fatal("binding group exceeded cap")
		}
		if !strings.Contains(st.SQL, "ON CONFLICT") {
			t.Fatal("lost upsert clause")
		}
	}
	if number(out[0].Args[2]) != 0 || number(out[1].Args[2]) != 100 || number(out[2].Args[2]) != 200 {
		t.Fatal("row order changed")
	}
}
func TestInvalidReworkDoesNotSendRequests(t *testing.T) {
	s := fresh(uuid, 1)
	s["bags"] = A{M{"id": 1, "status": "rework", "station": 1, "updated": 1, "measurements": A{}}}
	_, err := applyAction(s, "clear", M{"bag": 1, "mass": 2.3, "unit": "oz"}, "Tester", 2, "pin", uuid)
	if err == nil {
		t.Fatal("accepted invalid rework")
	}
	if len(arr(obj(arr(s["bags"])[0])["measurements"])) != 0 {
		t.Fatal("invalid reading added")
	}
}
