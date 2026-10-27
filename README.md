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
3. Supply `TURSO_DATABASE_URL`, `TURSO_AUTH_TOKEN`, and your chosen
   `TRAILMIX_SAFETY_PIN` when prompted. Keep them in Render environment
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
awake. The app reconnects after a restart; optional team-code sessions remain valid. The database remains at Turso independently
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
local replica. Station mode keeps an ordered browser save queue and clearly marks
local counts as pending until confirmed. If Turso is unavailable, saves remain
unconfirmed and can be retried with the same request ID. A missing remote database
configuration on Render stops startup instead of using a temporary local file.
Use one backend instance connected to this database. Export logs and make
restorable database backups regularly. An initial cloud installation starts
fresh; local records are not uploaded automatically.

## Prepare operators before production

Open **Operators**, enter a name, and expand **Preassign roles when adding
operators** to choose one or more roles. **Add several operators ahead of time**
accepts one name per line and applies the selected roles to each new person.
Use **Manage assignments** for individual changes. Device connections are not
needed to create the roster. Operators and assignments survive server restarts
and carry into the next run, including additional filling stations.

## Access and a stable Safety Rover PIN

The app opens directly without a team code or session token. Existing
`TRAILMIX_ACCESS_CODE` values do not enable the code gate by themselves. To
optionally restore it, set `TRAILMIX_REQUIRE_ACCESS_CODE=1` and set
`TRAILMIX_ACCESS_CODE`. Its browser session cookie also survives server restarts
with the same database.

Set `TRAILMIX_SAFETY_PIN` to your **current PIN before deploying** to keep that
exact value from an older version. An explicit environment PIN is saved in the
database and takes precedence. Without one, a PIN is generated once, printed on
its first startup, saved, and reused. A new empty database has new credentials.
Changing `TRAILMIX_SAFETY_PIN` changes the saved PIN. Credentials are separate from
production state and are not included in run exports. Anyone with the URL can
use ordinary production and operator controls; Safety decisions still require
the PIN.

## Faster station entry

On **all production pages**, including station mode, filling, delivery, inspection, rework, sealing, and final
inspection respond locally as soon as a valid action is entered. Saves run in
order in the background. Enter clears the weight field, advances to the next bag,
and keeps focus there. A previous save returning will preserve a new weight
already being typed.

The pending status distinguishes device entries from confirmed production data.
Other devices and the projector see confirmed counts. Keep the station open until
its pending count reaches zero. Pending entries are retained in that browser
profile across a reload, but are not a backup: clearing browser storage deletes
unsaved entries. A recovered queue requires **Retry saved entries** before new
entry. New entries stop when a save is unconfirmed or the queue fills.

The original action ID, operator, device, selected bags, and device entry time are
retained on retries. Server timestamps remain authoritative. The Events CSV also
includes the request ID, device entry timestamp, and station. Deliveries verify
the selected bags, and fills verify their mixing batch, rather than silently
applying an old action to new inventory. Ordinary unrelated station activity does
not interrupt a queued weight reading.

For a server-rejected action, inspect the held entries and reconcile the physical
bags. **Record review & clear held entries** saves their details and your reason
in the activity log before clearing them; it does not adjust bag counts. Unknown
save outcomes must be retried before this review option is available. Anyone can
report a safety issue while saves are pending; Safety Rover release still needs
the PIN.

Queued station actions are sent in batches of up to 50. Each batch uses three
Turso database round trips, retains a separate audit event and ID for every
action, and updates the run state once. The first action starts immediately;
entries accumulating during its save are combined into the next batch. Connected screens
share the server's committed state, so a new action does not trigger another
Turso read for every screen. Use one backend instance; external database changes
are not a supported way to edit production data. Cloud confirmations still take
network time; this update removes that wait from routine station entry.

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

Ordinary station access needs no team code by default; names are self-reported.
Anyone with the URL can use ordinary controls. Safety decisions
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

## Class roster, deletion and live setup

This release preloads 28 first names transcribed from the supplied class image.
Lucas, Blake and Charlie receive Safety; other class members are unassigned.
Names can be corrected in Operators > Manage assignments. On the first startup
after this deployment, the roster is merged into the current run and logged.
An existing unique first-name match is reused. The import runs once per database;
it does not re-create people you subsequently delete. Set TRAILMIX_SEED_ROSTER=0
to disable startup import.

Delete operator removes the person from active choices and unassigns their
devices. Their prior records remain. Deleted operators can be restored.

If you want the roster on the live app before deploying this release, open the
live app in your browser, open DevTools > Console, and paste APPLY_ROSTER_NOW.js.
It uses the existing roster APIs. This does not install batching or deletion;
those require deploying server.py and trailmix.html together.

Pending production actions use the same queue on full pages and station mode.
A failed batch retains all entries; unknown results are retried with their
original IDs. A rejected batch rolls back every new action in that batch.

## Personal delivery routing and updated weight band

Open **Operators → Manage assignments**. Assign Labeling → Filling and Filling → First inspection by filling station,
or for all filling stations. Assign each downstream leg once as a shared route. For a restricted
route, clear the broad Stocker, First inspection mover, and Final mover roles;
those existing roles intentionally cover all their corresponding legs.

Operators select their roster name and open **My deliveries**, or choose
**My delivery routes** in station mode. The older delivery station modes now
show this same personal route view. Assigned pickups are calculated from that
person's legs and filling stations. Pickup quantity defaults to all bags ready.
Each pickup creates a package on that person's load. **Delivered** receives only
that package at its destination, even if another mover has an older package on
the same leg. A person can finish their own package after an assignment change.
Station receipt controls reconcile the same package records, including partial
receipts. Existing in-transit bags and empty-bag pickups are imported once as
legacy packages. The projected board remains read-only and shows movers by leg
and filling station. Download **deliveries** for pickup/receipt history.

The weight band is now **2.00–2.25 oz net, inclusive**, with equivalent gram
validation. On deployment the active run is upgraded once, with an audit event.
Archived runs retain their own limits. Past readings keep their original limits
and pass/fail results in the weights export. Uncollected return-ready rework
outside the new band returns to the rework queue. Uncollected inspection-ready
bags outside the new band return to the inspection rework hold. Bags already
farther through production are not automatically reweighed.

Rework outside the band is rejected before it enters the local save queue and is
also rejected by the server. The field retains the reading and displays a
warning to correct the mass before delivery. Only an in-spec reading clears a
bag for direct delivery to Sealing; no return inspection is required.

The Station mode picker defaults to the production page currently displayed.
Shortcut menus describe actions rather than button labels. Delivery shortcuts
include the leg, filling station, and package position; binding IDs remain stable
as counts and package identifiers change. Shortcuts are registered for rendered
controls only, as before.

## Direct rework, saved setups and on-page assignments

The production flow is now:

- Labeling → Filling station → First inspection.
- Passing inspection → Sealing → Final inspection.
- Failed weight inspection → Rework → Sealing → Final inspection.

Rework must meet the 2.00–2.25 oz net band before clearance. It goes directly to
Sealing afterward. Downstream routes have no filling-station tags or separate
origin-station choices. Bag records still retain their filling origin for analysis.
On deployment, legacy downstream assignments are consolidated, and cleared
rework in transit is delivered to Sealing. Cleared rework already returned to
Inspection is made ready for a Sealing pickup without an extra weighing.

In **Settings → Saved configurations**, a Coordinator or Safety operator can
save the current setup under a name. Saving the same name offers an overwrite
confirmation. Up to 20 named setups are retained in the database and carried
across runs. They include filling-station definitions, active people and role /
delivery assignments, and device names and assignments. They do not include
inventory, bags, movements, event history, mixing batch IDs or credentials.

Under **End this run → Setup for next run**, choose **Keep current setup**
(the default) or a saved configuration before archiving. Existing reconciliation
and Safety PIN requirements still apply. A saved setup starts people as available
and leaves inventory and queues empty. New device profiles are retained as
unassigned rather than silently removed. Archives and existing logs are preserved.

A Coordinator or Safety operator sees a collapsed **Coordinator · assign an
operator** panel on each production station page, including station mode. The
Filling overview has a panel on each filling station. Select a person and assign
them here. Optional checkboxes replace existing assignments at that station and
apply the person / station to the current device. Other jobs remain assigned.
Delivery pages also offer the panel with a leg selector. Assignment changes are
logged; unfinished weight entry is preserved. Ordinary operators do not see
these coordination controls.
