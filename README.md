# TrailMix Production Desk

A shared web app for a small production line: filling stations, bag inventory,
weight inspection, overweight and underweight rework, sealing, final inspection,
transport queues, operator assignments, delivery dashboards, and recorded events.

**Ready to upload to GitHub. Hosting is configured but has not been deployed.**

## Put this on GitHub

1. Extract this ZIP.
2. Create an empty GitHub repository named `trailmix-production-desk` (private is fine).
   Leave the automatic README, license, and .gitignore options unchecked; these
   files are supplied, and no open-source license has been chosen for you.
3. Upload the **contents** of the extracted `trailmix-production-desk` folder.
   `server.py`, `trailmix.html`, `README.md`, and `render.yaml` must be directly
   at the repository root. Include `.gitignore`, `.gitattributes`, and the
   `.github` folder. Upload the extracted files, rather than the ZIP itself.
4. Commit with a message such as `Add production tracker`.

You can use GitHub's **Add file → Upload files** page, or GitHub Desktop to
create a repository from the extracted folder and publish it. Verify that
`.github/workflows/checks.yml` is present if you want automated checks.

## Deploy for free: Render + Turso

This version uses **Render Free** for the app and a separate **Turso Free libSQL
database** for persistent records. It needs no Render disk. You create the two
accounts and add the database credentials; no services have been created for you.

1. Create a **libSQL** database on [Turso](https://app.turso.tech), using its Free
   plan. Choose libSQL rather than the newer Turso engine for this version.
   Copy its database URL and create a read/write **database token**.
2. In the Render dashboard, choose **New → Blueprint** and connect this GitHub
   repository. Use the root `render.yaml`.
3. Supply `TURSO_DATABASE_URL`, `TURSO_AUTH_TOKEN`, `TRAILMIX_ACCESS_CODE`, and a
   separate `TRAILMIX_SAFETY_PIN` when prompted. Keep them in Render environment
   settings, never in GitHub files or browser code.
4. Confirm that the service is **Free**, with **no persistent disk** and **no
   Render Postgres service**, then deploy.
5. Open Render's HTTPS URL on every station and projector. Connect each browser
   once, then use **Devices** to assign a person and station remotely.
6. Test with two devices, export records, restart the Render service, reconnect,
   and confirm records remain before using it for a production session.

[Full setup, command-line alternative, limits, backups, and update instructions](REMOTE_HOSTING.txt).

Render Free sleeps after 15 minutes without incoming traffic, can take about a
minute to wake, and may restart. Its outbound SSE pings do not guarantee it stays
awake. Sign in again after a restart. The database remains at Turso independently
of the app. Free tiers have usage limits and no uptime guarantee. Keep both
accounts on Free and avoid enabling paid overages. See current pricing/limits
before signup; this package does not guarantee any provider's future pricing.

GitHub Pages alone cannot run this Python server. Render's own free PostgreSQL
expires after 30 days, so this configuration does not use it. Automatic app
deploys are disabled; deploy changes between production sessions. Blueprint
configuration changes can still restart a service.

## Where things live

| Item | Location |
| --- | --- |
| Source code | GitHub repository |
| Running app and HTTPS URL | Render Free web service |
| Production records, assignments, messages, and event history | Turso Free libSQL database |
| Database URL/token and access codes | Render environment settings |
| Device identity and keyboard preferences | Each browser profile |

The server talks directly to Turso over authenticated HTTPS. It does not save a
local replica or queue writes offline. If Turso is unavailable, saves remain
unconfirmed and can be retried with the same request ID. A missing remote database
configuration on Render stops startup instead of using a temporary local file.
Use one backend instance connected to this database. Export logs and make
restorable database backups regularly. An initial cloud installation starts
fresh; local records are not uploaded automatically.

## Run locally

Requires Python 3.10 or newer. No extra Python packages or Node.js are needed
for normal operation.

```sh
python server.py
```

On macOS/Linux, use `python3` if required. On Windows, you can also double-click
`START_WINDOWS.bat`. Open `http://localhost:8080` and use the code printed in the
server console. The local database defaults to `data/trailmix.sqlite3`.

[Station operation and detailed documentation](README.txt).

## Checks

Development checks require Python 3.10+ and Node.js 18+:

```sh
python run_checks.py
```

This runs local and remote-protocol backend tests, keyboard controls, and a live HTTP/DOM workflow using
an automatically created disposable database. It never points the workflow at
your production server. The same command runs in GitHub Actions on pushes and
pull requests. GitHub Actions runs tests; it does not host the production app.
Remote protocol tests use a local HTTP gateway with SQLite; no live Turso account
was available for cloud validation. The DOM tests do not replace a visual browser
check on your actual station devices.

## Access and records

Operators share a team access code; names are self-reported. Safety decisions
require the separate Safety PIN. Station and projector modes simplify the UI;
they are not separate account permissions. All accepted actions are saved with
server timestamps and operator/device attribution. Browser updates use one SSE
connection after sign-in; the access-code screen makes no recurring API requests.

The `.gitignore` excludes databases, exports, backups, and local environment
files. Keep real records and credentials outside the repository.

Official setup references:
[GitHub uploads](https://docs.github.com/en/repositories/working-with-files/managing-files/adding-a-file-to-a-repository),
[Render Blueprints](https://render.com/docs/infrastructure-as-code),
[Render Free limits](https://render.com/docs/free),
[Turso pricing](https://turso.tech/pricing),
[Turso SQL over HTTP](https://docs.turso.tech/sdk/http/reference).
