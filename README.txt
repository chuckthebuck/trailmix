TRAILMIX — COMPILED GO BACKEND

Read README.md and REMOTE_HOSTING.txt before updating.

Upload the complete extracted project contents to the existing GitHub repository,
including Dockerfile, go.mod, all .go files, testdata/ and trailmix.html. The
Docker build compiles Go; server.py is no longer the live entry point.

Keep the existing Turso database, credentials and Safety PIN settings. Let pending
saves finish and download a full JSON export before the first deployment.
Migration is automatic on the first Go startup. Refresh station browsers after
Render finishes deploying; retry retained entries using their original IDs.

Once Go has migrated and saved data, use Go for future deployments. The old
snapshot tables are kept read-only; rolling back to Python cannot continue the
live run safely.

Core production behavior and the roster, configurations, package assignments,
weights, inventories, audit exports and keyboard/station preferences are retained.
Routine saves now update changed current records and send incremental browser
updates. Full audit history remains available in exports.

Validation: Go compiled and vetted; 133 Python behavior comparisons, 101 backend
regressions, keyboard/patch tests and the browser workflow against both backends.
Real HTTP transaction, migration, interrupted-commit and restart tests use a
local SQLite gateway. Live Render/Turso timings have not been tested here.
