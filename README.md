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

## Deploy on Render

1. In the Render dashboard, choose **New → Blueprint** and connect this repository.
2. Use the root `render.yaml`. Review the paid web service and 1 GB persistent
   disk charges before deploying.
3. Supply `TRAILMIX_ACCESS_CODE` and a separate `TRAILMIX_SAFETY_PIN` when prompted.
   Set these in Render, never in GitHub source files.
4. Deploy, then open the HTTPS URL Render provides on every station and projector.
5. Connect each browser once. Open **Devices** to name it and assign a person
   and station. Use **Key bindings** inside station mode to customize controls.

[Full hosting, persistence, backup, and update instructions](REMOTE_HOSTING.txt).
GitHub Pages alone cannot run this Python server and shared database.
Automatic application deploys are disabled; deploy updates between production
sessions. Changes to Blueprint configuration can still cause a service restart.

## Where things live

| Item | Location |
| --- | --- |
| Source code | GitHub repository |
| Running app and HTTPS URL | Render web service |
| Production records and event history | SQLite on Render's persistent disk |
| Hosted database path | `/var/data/trailmix.sqlite3` |
| Access code and Safety PIN | Render environment settings |
| Device identity and keyboard preferences | Each browser profile |

Use one backend instance. Keep the disk attached across redeployments.
Export logs regularly and keep restorable database backups. An initial cloud
installation starts fresh; existing local records are not uploaded automatically.

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

This runs backend tests, keyboard controls, and a live HTTP/DOM workflow using
an automatically created disposable database. It never points the workflow at
your production server. The same command runs in GitHub Actions on pushes and
pull requests. GitHub Actions runs tests; it does not host the production app.
The DOM tests do not replace a visual browser check on your actual station devices.

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
[Render persistent disks](https://render.com/docs/disks).
