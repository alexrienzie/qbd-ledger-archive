# Running an extraction

## Before you start

1. **Make a copy of the company file** and work only from the copy. Close QuickBooks on every machine, copy the `.qbw` to a local folder on the QuickBooks machine, and compare the two hashes, which should match:

   ```powershell
   Get-FileHash "\\server\data\company.qbw", "C:\qb-archive\company.qbw" -Algorithm SHA256
   ```

   Keep the copy and its hash with the archive. It's the byte-level record of the file you extracted from.

2. **Use a machine that can stay logged in:** the SDK needs an interactive Windows session. A large file takes many hours, so pick a machine that won't sleep, restart for updates or log out partway through.

3. **Turn off anything else that talks to the file:** web connectors (bank feeds, card syncs, e-commerce integrations) hold sessions open on a schedule. Extraction coexists with them, but a sync landing at the wrong moment produces "file in use" errors.

4. **Install the tool** on the QuickBooks machine, as in the README's quick start (`py -m pip install -e ".[com]"` from the repository folder).

5. **Pick a home for the archive** outside any shared, synced or public folder. It will hold the company's complete books (see [SECURITY.md](../SECURITY.md)).

## Authorize the application (once)

Open the copy in QuickBooks as the Admin user, in single-user mode, then ask for the record counts, which is a small first request:

```powershell
qbd-archive extract --config archive.toml --phases probe
```

QuickBooks shows an application certificate dialog. Allow access always, including when QuickBooks isn't running (the wording varies by version), and pick the Admin user to log in as. Leave the box for personal data (social security and credit card numbers) unchecked; nothing here needs it. This is the only step that needs the QuickBooks screen. The grant persists, and later runs can launch QuickBooks on their own. The command ends with "NOT COMPLETE", which is expected: it only asked for counts.

If QuickBooks later says the application isn't authorized, the grant was revoked (in our experience after QuickBooks was force-quit several times). Re-approve it in QuickBooks under Edit > Preferences > Integrated Applications > Company Preferences, then rerun.

## Configure

Copy `archive.example.toml` to `archive.toml` and set:

- `transport.company_file`: the path to the copy, in single quotes (`'C:\qb-archive\company.qbw'`); double quotes treat the backslashes as escapes
- `books.start`: the first day of the first fiscal year with any activity. Validation fails if earlier years exist and this is set later (see `fiscal_year_detail` below).
- `books.end`: the last day of books activity. Anything later lands in the "post" catch-all.
- `books.fiscal_year_start_month`: must match the company file's own fiscal year setting (Company > My Company in QuickBooks); validation checks it
- `extract.exclude_txn_types = ["transferinventory"]` on QuickBooks Pro or Premier, which don't have inventory transfers

## Run

```powershell
qbd-archive run --config archive.toml > extract.log 2>&1
```

That runs extract, load and validate in sequence; keeping the output in a log file helps if something goes wrong overnight. You can also run each step on its own (`extract`, `load`, `validate`), and `status` shows what's done and what's outstanding. Extraction is resumable: stop it any time (Ctrl+C), and the next run skips everything already saved. If extraction stops early, `run` stops before loading; `load` and `validate` still work on whatever has been saved, which helps when diagnosing.

### How long it takes

Most of the time goes into QuickBooks materializing queries; transfer is quick. Rough rates from the original build, on an older desktop machine:

- general ledger detail: one to two minutes per 20,000 lines
- invoices with line items: one to one and a half minutes per 1,000
- a QuickBooks cold start: one to two minutes, absorbed by the warm-up request

A file with many years of history took several overnight sessions.

## When things stall

| Symptom | What's happening | What to do |
|---|---|---|
| `A modal dialog box is showing` | A QuickBooks window is waiting for a click (a popup, or the startup screen when QuickBooks launched with the machine) | Dismiss it on the QuickBooks machine. Reports and lists retry after a pause; a document window that fails is recorded as a gap and pulled again on the next run. |
| `Could not start QuickBooks` | QuickBooks is still starting, or still shutting down from the last request | Usually nothing; the extractor pauses and retries |
| Timeouts on one document type | The windows are too heavy | Nothing; the extractor splits them, down to single days. To save time, set a smaller starting window in `[extract.window_months]` |
| Timeouts on a ledger month | One month's ledger takes longer than `report_timeout` | Raise `report_timeout` and rerun; ledger months aren't split |
| `parsing the provided XML` on a document type | QuickBooks rejected the request itself | The extractor stops. It's a bug in the request for that type, or a type your edition doesn't support; please report it |
| A type is skipped as "QuickBooks didn't count it" | Your edition doesn't have that document type | Add it to `exclude_txn_types` |
| `not authorized`, `re-authorize` or similar | The access grant was revoked | Re-approve the application (see above) |
| Extractor stops after repeated failures | A persistent blocker: QuickBooks closed, a dialog nobody dismissed, the file in use | Clear it and rerun |
| "another extractor holds extract.lock" | A run is in progress, or a stopped run left its lock | If no extractor is running, delete `extract.lock` in the archive folder |
| File in use for everyone | Leftover QuickBooks processes, or ghost sessions on a database server | Close QuickBooks normally. On a hosted file, restarting the QuickBooks database server service cleared ghost sessions for us. |

Don't force-quit `qbw.exe` as routine recovery. See [lessons.md](lessons.md) for why.

## Supervising an unattended run

The extractor logs every line with a timestamp and touches `extract.lock` every 30 seconds while it runs. For an unattended overnight run, put something outside the process in charge: a small scheduled script that checks the log file's age and, if nothing has been written for 30 minutes, ends the extractor process, deletes `extract.lock` and starts it again. The manifest makes restarts cost nothing. The run is done when `EXTRACT_COMPLETE` appears in the archive folder.

## Reading the validation results

After `run` (or `load` then `validate`), open `validation/summary.md`. Every check should say pass; warnings don't fail the run but deserve a look.

| Check that fails | Where to look | Usual fix |
|---|---|---|
| `plan_coverage` | The listed chunk names | Run `extract` again to fill them in; `status` lists what's outstanding |
| `tieout` | `tieout_<basis>_<period>.csv`, rows where `tied` is False | Find the account and period, then trace its rows in `gl_line` by `src_key` to the raw response |
| `month_continuity` | `month_continuity.csv` | Usually a misplaced row inside the month; check `accounts_placed` |
| `fiscal_year_detail` | The chunk named | Move `books.start` back to the first fiscal year with activity, then rerun |
| `pnl_net_income` | `pnl_net_income.csv` | Compare the period's accounts by type; a wrong account type or missing P&L detail |
| `fiscal_year_setting` | The months named | Fix `fiscal_year_start_month`, then rerun |
| `accounts_placed` | `accounts_unplaced.csv` | The rows' Account column and the raw report around them |
| `completeness` | `completeness.csv` | Rerun `extract` for missing windows; a variance with no gaps needs a closer look |
| `line_detail`, `no_conflicts`, `lists_clean` | The detail in `summary.md`; `conflicts.csv` | Report it (without company data) |
| `crosswalk` (warning) | `crosswalk_orphans.csv` | Expected for types this tool doesn't extract, such as payroll |

## Keep

- `raw/` and `manifest.json` (the source of record)
- the company-file copy and its hash
- `validation/` (the evidence)

The SQLite file can always be regenerated from `raw/` with `load`.
