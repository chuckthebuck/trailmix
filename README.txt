TRAILMIX PRODUCTION DESK

FREE HOSTING EDITION
Use Render Free + a separate Turso Free libSQL database. See README.md and
REMOTE_HOSTING.txt first for cloud setup. No paid Render disk is required.
The local SQLite instructions below still apply when running on your own PC.

A browser-based production tracker for the supplied Trail Mix Project SOP,
with the requested amendment: both underweight and overweight bags go to
weight rework. Rework clearance always requires a return to First Inspection.

QUICK START ON WINDOWS

1. Extract the entire ZIP into a folder on the computer that will act as server.
2. Install Python 3 if this computer does not already have it. No extra Python
   packages are needed. The station devices need only their web browsers.
3. Double-click START_WINDOWS.bat. Leave that window and computer running.
4. Open http://localhost:8080 on the server computer.
5. Enter the team access code printed in the server window, then your name.
   Keep the separately printed Safety Rover PIN with Safety personnel.
6. On other devices on the same network, open http://SERVER-IP:8080, replacing
   SERVER-IP with this computer's local IPv4 address (shown by Windows ipconfig).
   If Windows asks about network access, allow it on the trusted private network.

The app is implemented and runnable; it has not been published to an Internet
URL. Everyone uses the same server URL, with no station-side installation.
On campus/guest Wi-Fi, device isolation may prevent local devices from reaching
each other. Use an approved network or deploy this server to an HTTPS host with
persistent storage. Do a short practice run on the actual network first.

MAC OR LINUX

From this folder: python3 server.py
Or: sh start.sh
Python 3.10+ is recommended. Only the standard library is used.

STATION MODE AND FAST WEIGHT ENTRY

After connecting, tap Station mode at the top, select the station, then tap
Enter station mode. Filling cards also have a Use this station on this device
button. Each device remembers its selection across reloads and reconnections.
Exit station mode returns to the full dashboard. Station mode simplifies the
screen; it is not a separate account or a security restriction.

Station mode hides navigation, exports, analytics, and other stations. Filling
shows only the selected station's bag inventory, fill button, pickup quantity,
and mixing batch assignment (collapsed after assignment). Inspection, Rework,
Sealing, and Final Inspection show one bag at a time. Each station also has
its own incoming receipt and outgoing pickup buttons, including both Inspection
outputs, the return from Rework, and Sealing to Final Inspection. Bag Stocker, First
Inspection Mover, and Final Mover have their own optional handoff controls. The operator
name, connection status, and Report safety issue remain available.

KEYBOARD CONTROLS IN STATION MODE

Each control shows its current shortcut. Plain letter shortcuts work when focus
is outside a text field; Alt plus the displayed key also works while typing.
In a weight field, letters other than E can activate shortcuts while numbers,
decimals, signs, and E remain available for mass entry. Enter records the mass.
Held keys never repeatedly record operations. Tab / Shift+Tab navigate normally.

Typical keys: F fill, A add labels / seal / pass, X fail, P pickup, R receive,
S Inspection pickup to Sealing, V receive returns, W focus weight, O operator,
E exit station mode, H safety report, K open Key bindings. Additional controls
receive distinct keys; their visible labels are authoritative.

Choose Key bindings in station mode to change a control's key, remove a binding,
or restore defaults. Settings are remembered per browser profile and station.
Letters, digits, Space and Shift/Alt combinations are supported. Conflicts are
rejected. Enter remains reserved for mass submission. Shortcuts pause inside
normal dialogs and honor disabled controls, queue order, and server checks.
No Node.js installation is needed on production devices: test_keyboard.cjs is
a development test file, not a runtime dependency.

At First Inspection, label checks default to pass. Tap Label failed only when
needed; it marks the current bag as a label failure, and Record mass discards
that bag. Undo label failure clears the mark before submission. The mark resets
for the next bag. A label check result is still logged on every inspection.

Enter submits a weight reading in Inspection or Rework, including station mode.
The cursor returns to the empty weight box for the next bag after a successful
save, when a bag is available and the operator name has been entered. Changing
mass units or marking a label failure also returns focus to the weight box.
Background updates wait while a mass is partly entered; they can refresh an
empty focused weight box to show newly received bags. Another active input,
including operator name, is not interrupted. A failed or unconfirmed save never
advances the bag. Verify any stale-data conflict before re-entering a reading.

OPERATOR ASSIGNMENTS AND MESSAGES

Open Operators to add each person with a distinct name. Select Manage to assign
one or more jobs (for example Bag Stocker plus First Inspection Mover), and set
Available, Working, Break, or Off. People choose their roster name in the operator
name field on their device. Assignments and messages appear above their station
controls. Assignment management uses the existing team access code; individual
operator names remain self-reported rather than separate authenticated accounts.

The roster distinguishes assigned roles from last recorded action and location.
These are logged operations, not live location or proof someone is physically at
a station. Activity events record the person's ID and assignments at action time.
People CSV summarizes recorded work. All assignment changes preserve before/after
values in the event history; the roster carries into the next run.

Send messages to a named person, a station, the delivery team, or everyone.
Normal and urgent messages appear on relevant devices. Recipients can acknowledge
messages; the operator view shows who acknowledged them. A manager can resolve a
message without deleting its history. Messages, acknowledgements, and resolutions
are retained in exports. Urgent priority is a display flag: use Report safety
issue when a production hold is needed.

Assignment/message updates can appear while a weight is partly typed, without
clearing it. Acknowledging a message preserves that reading when the same bag is
still first in its queue. Coordination-only updates do not invalidate an entered
production reading; conflicting actual production actions still require review.
Active messages belong to the run. A new run starts with an empty message list;
old messages and their acknowledgements remain available in the archived run.

DELIVERY DASHBOARDS AND PROJECTOR DISPLAY

Open Delivery dashboard for a read-only overview. Large buttons show All routes,
Bag Stocker, First Inspection Mover, or Final Mover; delivery screens use no
operational dropdowns. The board shows labeled bags ready, each filling station's
empty stock and incoming deliveries, filled pickup counts per filling station,
other pickup queues, in-transit counts, oldest waiting times, and assigned movers.
An empty filling supply is marked Needs labeled bags. Counts come from recorded
pickups/receipts, not automatic physical sensing.

First Inspection Mover station mode shows a card for EVERY filling station,
including the ready count, oldest wait, and bags in transit. Pick up N bags moves
all ready bags from that specific station only. Delivered N bags receives only
that station's in-transit bags. No station dropdown or combined pickup hides the
source of the bags. The other delivery roles also show their destinations and
routes directly. Existing station-page pickup/receipt controls still work.

Select Project this board for a large, high-contrast display with no production
entry controls. It refreshes automatically, flags stale/disconnected data, and
remembers display mode after a reload. Escape or Exit display returns to the
normal delivery page. You can also open the server URL with ?display=delivery,
then enter the team code on that display device if requested. This lets delivery
staff read queues without touching a production workstation.

The projector shows messages explicitly addressed to Everyone, Delivery team,
or one of the delivery roles. Messages addressed to individual people are not
projected. The normal Operators page can manage all messages. Projector mode is
a read-only interface, not a separate server permission or separate login type.
A host can exit it when management or entry is needed. Formal hygiene procedures
still apply to any device used near production.

DEVICE ASSIGNMENTS

Devices lists browsers that have connected to this server. Name each browser,
assign a roster person, and select its station or delivery display remotely.
The assignment applies on that browser; an unfinished weight or open dialog
waits until entry is finished. Activity records include the device ID and name
at action time. Recently seen indicates an active connection within about 45
seconds, not proof of physical attendance. This is not a network/device scanner.
Different profiles have separate IDs; tabs in one profile share an ID.

UPDATING AN EXISTING INSTALLATION

Stop the server. Back up the data folder, then replace BOTH server.py and
trailmix.html with this release and restart. Keep your existing data folder.
Older database states receive the new people/message/device fields automatically on
load; existing bags, readings, events, and archived runs remain intact. Refresh
station browsers after the server restarts and reconnect with the printed code.

NORMAL STATION FLOW

Labeling: add labeled empty bags; record a quantity picked up for a station.
Filling: receive that delivery, assign the mixing batch ID printed on the tub,
then tap Filled 1 bag for each bag. Pickup starts the trip to First Inspection.
Inspection: receive incoming bags, enter NET mass in oz or g, and record the
label check. Accepted net range is 1.50 through 1.75 oz, inclusive. Use an empty
labeled bag to tare the scale. Weight failures are held at inspection; label
failures are discarded, including bags with both a label and weight problem.
Rework: pickup and receive the held batch. Adjust high or low mass, record each
reading, then clear in FIFO order when within range. Cleared bags wait for a
pickup back to First Inspection. They cannot go directly to Sealing.
Sealing: receive only bags released by First Inspection; record sealed or failed
seal. Failed seals require Safety Rover disposition before another attempt.
All pickup quantities default to the entire ready quantity at their source,
including labeling, each individual filling station, inspection, rework, and
sealing. Edit the quantity only for a partial pickup.

Every production station has its own pickups and receipts, including the return
from Rework. Final Inspection has a separate simple page; there is no combined
transit page required for production. The optional Final Mover station mode
shows each handoff directly, without route dropdowns.
Final inspection assumes seal, label, leaks/damage, trapped product, and
contamination checks pass. Tap Pass to release the next bag, or Fail inspection
and enter a reason to hold it for Safety. No checklist or reason is required
for a pass. Each pass still records the five passing criteria in the event log.
Safety & holds: anyone may report a food safety issue, which holds production.
Safety Rover release and product dispositions require the separate PIN. Safety
can reseal an acceptable bag/label, return it to inspection, discard it, or
rebag into an available labeled replacement at its original filling station.
Rebagging creates a new bag record linked to the old discarded bag. Spot checks
retain the requested reason and measured weight; failures place product on hold.

Bag IDs are virtual records assigned by the app. It does not print labels or
scan barcodes. Keep physical bags in the displayed queue order and identify held
bags consistently. A shared name is not individual account authentication;
operator names are self-reported. The Safety PIN restricts Safety decisions.

The SOP incident/contamination forms remain the formal Safety documents.
Mixing batch identity is recorded per bag, but ingredient weighing and checklist
completion are not digitized in this version. The app tracks only operations
that people record; it is not connected to scales, counters, or sealing hardware.

RECORDS AND ANALYSIS

Every accepted production action is timestamped by the server, attributed to the
entered operator, and saved transactionally in SQLite. Logs include quantities,
bag IDs, device identity, source/destination, batch identity, readings, failure reasons, Safety
decisions, submitted values, and affected bag states before/after each action.
Safety PINs and team access codes are excluded from production records.

No production action deletes earlier event history. The database rejects updates
or deletes to the event table. Correction notes add a record; they do not rewrite
earlier measurements or automatically change calculated statistics. Include the
bag ID and original event time when noting an error, and account for it during
analysis. This is an operational audit trail, not a tamper-proof certification;
someone with access to the server files controls the database.

Settings offers seven exports for the active run and every archived run:
- Events CSV: complete action history, input fields, and before/after bag states.
- Weights CSV: one row per initial inspection, repeat inspection, rework reading,
  or Safety-requested spot check, with both entered and normalized units.
- Bags CSV: station, mixing batch, final state, first weight-failure flag,
  seal failure count, rebag links, and fill-to-completion time.
- People CSV: assignments, manual status, last action, and per-person action totals.
- Messages CSV: message audiences, text, priority, acknowledgements, and resolution.
- Devices CSV: names, assignments, and last observed browser use.
- JSON: complete state and event history, including people, messages, and devices.

Dashboard definitions:
- Bags filled: number of bag records, including labeled replacement bags.
- Weight rework percentage: bags failing their first independent weight check
  divided by bags receiving their first independent inspection. A simultaneous
  label/weight failure is included in weight failures but is discarded.
- Completed/hour: currently completed bags divided by wall-clock hours since
  the run began, with a one-minute minimum denominator. Includes downtime.
- Mean first weight: first independent inspection mass per inspected bag.
- Output bars: final release events in consecutive five-minute windows over the
  last 60 minutes. A later Safety hold does not erase an earlier release event;
  the currently completed count excludes bags returned to a hold.
- Seal failures: failed sealing attempts; final-check failures are separately
  logged and should be included separately in downstream quality analysis.

SERVER BEHAVIOR

After sign-in, one server-sent events connection pushes changes as they happen.
An idle connection receives a keepalive about every 20 seconds; there are no
recurring two-second requests. No live connection or recurring API requests run
while waiting for the team access code. Background changes preserve partly
entered masses and other active fields. Local display clocks do not make HTTP
requests. Interrupted live connections reconnect automatically; a Reconnect
button is also available. Native EventSource support is required for live updates.

If another production action changes the run before a save, the server rejects
the stale action and loads current data. Review the queue and re-enter the
reading or quantity before retrying. Assignment/message-only changes do not
invalidate an entered production reading.

No offline write queue is used. If a save's response is lost, use Retry the same
save and do not repeat the physical operation. Request IDs prevent duplicate
records if the original save succeeded. Keep that browser tab open until the save
is resolved. After an interrupted/reloaded tab, check Activity before repeating
an action whose outcome is uncertain. Local SQLite survives local server restarts. Hosted Turso records survive Render restarts.

At run end, Safety reconciles unfinished product and remaining empty inventory.
The reconciliation records all unfinished filled bags as discarded and records
the empty inventory disposition in the reason. Archive & start new run preserves
the complete old run; it does not erase it. Do not use reconciliation mid-run as
an inventory adjustment.

BACKUPS

The default LOCAL database is data/trailmix.sqlite3 beside server.py. Hosted
records instead live in Turso; see REMOTE_HOSTING.txt for its backup procedure.
Export records
regularly. For a complete restorable backup, stop the server cleanly and copy the
entire data folder. To restore, stop the server, replace that folder with a saved
copy, then restart. Do not use the SQLite files from the test environment.
JSON exports support analysis; a JSON import/restore UI is not implemented.

OPTIONAL CONFIGURATION

TRAILMIX_ACCESS_CODE sets a stable team access code; otherwise a new random code
is printed each time the server starts. TRAILMIX_SAFETY_PIN similarly sets a
stable Safety PIN. Existing browser sessions must reconnect after a restart.
TRAILMIX_DB sets the database path; --db also sets it. --port or PORT sets the
port; the default is 8080. Bind defaults to 0.0.0.0 for access from station devices.

For FREE remote deployment: render.yaml selects Render Free with an external
Turso libSQL database. Set TURSO_DATABASE_URL, TURSO_AUTH_TOKEN, the team code,
and Safety PIN in Render. No persistent Render disk is used. See README.md and
REMOTE_HOSTING.txt. A missing remote database configuration on Render stops
startup rather than allowing records to be written to temporary storage.
No live deployment or cloud account has been created by this package.

The included Dockerfile is optional for local deployments with a Docker volume:
  docker build -t trailmix .
  docker run --rm -p 8080:8080 -v trailmix-data:/data trailmix
Do not set TRAILMIX_HTTPS=1 on a plain HTTP local deployment.

VALIDATION

Run: python -m unittest discover -p "test_*.py" -v
Optional keyboard checks, if Node.js is installed: node test_keyboard.cjs
Optional live workflow harness: node test_workflow.cjs. This WRITES test records:
use only a disposable server/database on port 8081 with test team code
"test-access" and test Safety PIN "123456". TRAILMIX_TEST_URL can change its URL.
Never run that workflow harness against a real production database.
The included tests cover inventory, handoffs, high/low rework, independent return
inspection, label disposal, mass conversion, inclusive limits, duplicate saves,
concurrent write conflicts, Safety controls, final checks, rebag lineage, append-
only records, archives, persistence, devices, authenticated live updates, remote HTTP transactions, and
CSV formula protection.

Frontend syntax and live HTTP/DOM-flow checks were performed. A full Chromium
visual check could not be run in the build environment because downloading the
browser was blocked. Validate the layout and network access on your station
devices during a practice run before using it for production records.
