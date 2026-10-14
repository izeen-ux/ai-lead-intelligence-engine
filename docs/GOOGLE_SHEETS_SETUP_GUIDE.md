# Google Sheets output — setup guide (beginner-friendly)

The project writes its lead list into a Google Sheet. It uses a **service account** (a robot
Google identity) — the same approach that already worked in the pre-Phase-5 validation.

> **Security:** the service-account key file is a password. Never paste its contents into a
> chat, an email, a document or a screenshot, and never commit it. It lives only in
> `secrets/google/` (git-ignored).

## Current status (verified 2026-10-01)

Already working on this computer — nothing to do:

| Item | State |
|---|---|
| Key file `secrets/google/service-account.json` | present, accepted by Google |
| `GOOGLE_SHEETS_SPREADSHEET_ID` in `.env` | set |
| Sheet shared with the service account | yes (read + write succeeded) |

## If you ever need to set it up again

1. **Key file:** in Google Cloud Console → *IAM & Admin → Service Accounts* → your service
   account → *Keys → Add key → JSON*. Save the downloaded file as
   `secrets/google/service-account.json`. Make sure the *Google Sheets API* is enabled for
   that project (*APIs & Services → Library → Google Sheets API → Enable*).
2. **Share the Sheet:** open the Google Sheet → *Share* → paste the service account's email
   (the export command prints it, it ends in `.iam.gserviceaccount.com`) → role **Editor** →
   *Send*.
3. **Spreadsheet id:** it is the long part of the Sheet's address between `/d/` and `/edit`.
   Put it in `.env`:
   ```
   GOOGLE_SHEETS_SPREADSHEET_ID=<the id>
   ```
4. **Check (reads only, writes nothing):**
   ```powershell
   uv run python scripts/export_leads_to_sheets.py --db --check
   ```

## What the error messages mean

| Message | What to do |
|---|---|
| `service-account key file not found` | step 1 (file name and folder must match) |
| `Google Sheets refused access (HTTP 403)` | step 2 — share the Sheet with the service-account email as Editor |
| `spreadsheet not found` | step 3 — check the id |
| `could not obtain a Google access token` | the key was deleted or disabled — create a new key (step 1) |
| `sheet_header_mismatch` | a tab with the same name already holds other data — rename that tab or change `GOOGLE_SHEETS_WORKSHEET` |

The app creates the `Leads`, `Review_Queue` and `Export_Runs` tabs itself. You can read, sort
and filter them, but **edits in the Sheet are not saved back** — the next export overwrites a
changed lead row with the database values. Make review decisions in the dashboard instead.
