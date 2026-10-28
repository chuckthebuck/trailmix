# TrailMix Production Desk — Go backend

A shared production tracker for labeling, filling, first inspection, rework,
sealing, final inspection, deliveries, operator assignments and safety holds.

The live backend is a compiled Go binary. Python remains only as a reference
implementation and development test fixture; the Docker image contains no Python.

## Update the existing GitHub / Render service

1. Let pending station saves finish and download a complete JSON export from
   the app. Keep that export as a backup before the first Go deployment.
2. Extract the ZIP and upload the **contents** of `trailmix-main` to the existing
   repository root. Include `Dockerfile`, `go.mod`, **all `.go` files**,
   `testdata/`, `trailmix.html`, and the supplied checks/documentation. Uploading
   only `server.py` or `trailmix.html` does not install this backend.
3. Keep the existing `TURSO_DATABASE_URL`, `TURSO_AUTH_TOKEN`, optional team
   sign-in settings and Safety PIN environment settings. Do not create or reset
   the database. No secrets belong in GitHub.
4. Deploy using the supplied Dockerfile. The build runs the Go tests and compiles
   the server. The supplied `render.yaml` now uses Docker, one instance, and
   `/healthz` for health checks. If the existing service already builds its
   Dockerfile, keep that service and its URL.
5. The first Go startup migrates old plain or compressed snapshots automatically.
   After deployment, refresh all station browsers. Retained outbox entries keep
   their original request IDs and can use **Retry saved entries**.

The new startup log begins `TrailMix Go is running`. HTTP responses also include
`X-TrailMix-Backend: go-normalized-v1`.

## What changed

- Current bags, station inventory, operators, devices, messages, configurations
  and delivery packages are separate database records. Saves write changed
  records plus append-only audit events; they do not read or rewrite the run's
  historical snapshot.
- A batch of up to 50 production actions commits atomically in three database
  round trips. A fully confirmed retry uses two and writes nothing.
- Browser acknowledgements and live updates carry revisioned patches. A missing
  revision triggers a full refresh of confirmed state. Local previews remain
  separate from confirmed state, and retries retain original action IDs.
- Screens can read the last committed cache while a database save is in progress.
  New state is published only after a confirmed commit.
- Full before/after bag records remain in audit storage and JSON/CSV exports.
  Normal screen updates contain event summaries, not audit bag snapshots.

Production behavior is retained: 2.00–2.25 oz net inclusive, out-of-spec rework
rejected, cleared rework directly to Sealing, per-person delivery assignments,
separate pickup/delivery acknowledgements, saved next-run configurations,
coordinator assignment panels, station modes and configurable keyboard controls.
The roster, existing packages, inventories, request IDs, PIN and optional sign-in
cookie migrate without being reset.

## Migration and rollback

The migration is transactional. Original run snapshots and audit rows are kept;
new `tm_*` tables hold current records, summaries and request results. Once
migrated, old snapshot tables are read-only to prevent an old Python deployment
from writing stale state. **Do not roll back to the Python server after Go has
started saving actions.** Use this Go backend or a compatible later version.

Keep one backend instance. Database revision checks reject stale cached writers,
but live notifications are intentionally owned by one service instance.

## Development

Install Go 1.23+ (tested with 1.27.3), Python 3.12+ and Node.js 18+. The Go module
uses only the standard library and downloads no third-party modules.

```sh
python3 run_checks.py
```

This runs Go tests and vet, 133 behavior comparisons captured from Python,
legacy regression tests, real HTTP migration/rollback/retry tests using temporary
SQLite, keyboard and patch-merging checks, and the complete browser workflow
against both Go and Python. No production database is used.

For a normal build:

```sh
CGO_ENABLED=0 go build -buildvcs=false -trimpath -o trailmix .
```

Set the existing Turso environment variables before starting `./trailmix`.
The Go runtime requires persistent Turso storage; it does not fall back to a
local file. `go_gateway.py` is a development-only temporary SQLite HTTP gateway.
The loopback-only HTTP exception requires `TRAILMIX_TEST_DB=1`; never set that
flag for the live service.

See `REMOTE_HOSTING.txt` for deployment details and `UPDATE_NOTES.txt` for
validation and local performance measurements. Cloud latency remains unverified
until this version is deployed and measured on the live service.
