package main

import (
	"bytes"
	"context"
	"encoding/csv"
	"fmt"
	"net/http"
	"strings"
	"time"
)

func (a *application) export(w http.ResponseWriter, r *http.Request, ctx context.Context) {
	kind := r.URL.Query().Get("kind")
	if kind == "" {
		kind = "events"
	}
	s, err := a.engine.audit(ctx, r.URL.Query().Get("run"))
	if err != nil {
		failed(w, r, err)
		return
	}
	mime, raw, err := exportData(s, kind)
	if err != nil {
		failed(w, r, err)
		return
	}
	extension := "csv"
	if kind == "json" {
		extension = "json"
	}
	w.Header().Set("Content-Type", mime)
	w.Header().Set("Content-Disposition", fmt.Sprintf(`attachment; filename="trailmix-%s.%s"`, kind, extension))
	w.Header().Set("Content-Length", fmt.Sprint(len(raw)))
	w.WriteHeader(200)
	_, _ = w.Write(raw)
}
func exportData(s M, kind string) (string, []byte, error) {
	if kind == "json" {
		return "application/json", encode(s), nil
	}
	rows := [][]any{}
	run := s["runId"]
	j := func(v any) string { return string(encode(v)) }
	switch kind {
	case "events":
		rows = append(rows, []any{"event_id", "run_id", "utc_time", "unix_ms", "operator", "person_id", "assigned_roles_json", "device_id", "device_name", "action", "quantity", "bag_ids", "detail", "submitted_data_json", "changes_json", "before_json", "after_json", "request_id", "device_recorded_ms", "recorded_station"})
		for _, v := range arr(s["events"]) {
			e := obj(v)
			ids := []string{}
			for _, id := range arr(e["bags"]) {
				ids = append(ids, text(id))
			}
			rows = append(rows, []any{e["id"], run, time.UnixMilli(integer(e["at"])).UTC().Format("2006-01-02T15:04:05Z"), e["at"], e["operator"], e["personId"], j(e["assignments"]), e["deviceId"], e["deviceName"], e["type"], e["qty"], strings.Join(ids, "|"), e["detail"], j(e["data"]), j(e["changes"]), j(e["before"]), j(e["after"]), e["requestId"], e["recordedAt"], e["recordedStation"]})
		}
	case "bags":
		rows = append(rows, []any{"run_id", "bag_id", "filling_station", "mix_batch", "status", "created_ms", "completed_ms", "cycle_seconds", "first_weight_failed", "seal_failures", "replaces", "replaced_by"})
		for _, v := range arr(s["bags"]) {
			b := obj(v)
			var cycle any
			if truth(b["completedAt"]) {
				cycle = (number(b["completedAt"]) - number(b["created"])) / 1000
			}
			rows = append(rows, []any{run, b["id"], b["station"], b["mixBatch"], b["status"], b["created"], b["completedAt"], cycle, b["firstFail"], b["sealFails"], b["replaces"], b["replacedBy"]})
		}
	case "weights":
		rows = append(rows, []any{"run_id", "bag_id", "filling_station", "mix_batch", "measurement_id", "kind", "utc_time", "operator", "entered_value", "entered_unit", "net_oz", "net_g", "in_spec"})
		for _, v := range arr(s["bags"]) {
			b := obj(v)
			for _, vv := range arr(b["measurements"]) {
				m := obj(vv)
				min, max, pass := m["minOz"], m["maxOz"], m["passed"]
				if min == nil {
					min = obj(s["limits"])["min"]
				}
				if max == nil {
					max = obj(s["limits"])["max"]
				}
				if pass == nil {
					pass = number(min)-1e-9 <= number(m["oz"]) && number(m["oz"]) <= number(max)+1e-9
				}
				rows = append(rows, []any{run, b["id"], b["station"], b["mixBatch"], m["id"], m["kind"], time.UnixMilli(integer(m["at"])).UTC().Format("2006-01-02T15:04:05Z"), m["operator"], m["entered"], m["unit"], m["oz"], number(m["oz"]) * 28.349523125, pass})
			}
		}
	case "deliveries":
		rows = append(rows, []any{"run_id", "delivery_id", "source", "filling_station", "quantity", "picked_by", "person_id", "picked_ms", "status", "delivered_ms", "delivered_by", "bag_ids_json", "remaining_bag_ids_json", "receipts_json"})
		for _, v := range arr(s["deliveries"]) {
			d := obj(v)
			rows = append(rows, []any{run, d["id"], d["source"], d["station"], d["qty"], d["pickedBy"], d["personId"], d["pickedAt"], d["status"], d["deliveredAt"], d["deliveredBy"], j(d["bagIds"]), j(d["remainingBagIds"]), j(d["receipts"])})
		}
	case "devices":
		rows = append(rows, []any{"run_id", "device_id", "name", "assigned_person_id", "assigned_view", "configured_ms", "configured_by", "last_seen_ms", "last_reported_view", "last_reported_operator"})
		for _, v := range arr(s["devices"]) {
			d := obj(v)
			rows = append(rows, []any{run, d["id"], d["name"], d["personId"], d["mode"], d["updatedAt"], d["updatedBy"], d["lastSeen"], d["observedMode"], d["observedOperator"]})
		}
	case "messages":
		rows = append(rows, []any{"run_id", "message_id", "audience_type", "audience_id", "text", "priority", "created_ms", "created_by", "acknowledgements_json", "resolved_ms", "resolved_by"})
		for _, v := range arr(s["messages"]) {
			m := obj(v)
			rows = append(rows, []any{run, m["id"], m["targetType"], m["target"], m["text"], m["priority"], m["createdAt"], m["createdBy"], j(m["acknowledgements"]), m["resolvedAt"], m["resolvedBy"]})
		}
	case "people":
		rows = append(rows, []any{"run_id", "person_id", "name", "status", "assigned_roles_json", "updated_ms", "last_action_ms", "recorded_actions", "bags_filled", "inspection_readings", "rework_readings", "final_passes"})
		for _, v := range arr(s["people"]) {
			p := obj(v)
			counts := map[string]int64{}
			var last any
			count := 0
			for _, vv := range arr(s["events"]) {
				e := obj(vv)
				if same(e["personId"], p["id"]) || strings.EqualFold(text(e["operator"]), text(p["name"])) {
					count++
					last = e["at"]
					counts[text(e["action"])] += integer(e["qty"])
				}
			}
			rows = append(rows, []any{run, p["id"], p["name"], p["status"], j(p["assignments"]), p["updatedAt"], last, count, counts["fill"], counts["weigh"], counts["clear"], counts["finish"]})
		}
	default:
		return "", nil, problem{"Unknown export format."}
	}
	var out bytes.Buffer
	out.Write([]byte{0xef, 0xbb, 0xbf})
	writer := csv.NewWriter(&out)
	writer.UseCRLF = true
	for _, row := range rows {
		record := []string{}
		for _, v := range row {
			value := text(v)
			if b, ok := v.(bool); ok {
				if b {
					value = "True"
				} else {
					value = "False"
				}
			}
			if _, ok := v.(string); ok && len(value) > 0 && strings.ContainsRune("=+-@\t\r", rune(value[0])) {
				value = "'" + value
			}
			record = append(record, value)
		}
		if err := writer.Write(record); err != nil {
			return "", nil, err
		}
	}
	writer.Flush()
	return "text/csv; charset=utf-8", out.Bytes(), writer.Error()
}
