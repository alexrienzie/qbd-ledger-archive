# Known limitations

What hasn't been proven, and where to look first if a run against a new company file goes wrong. If you're using an AI coding agent to set this up, point it at this page.

## How the code was tested

- **Loader, parsers and checks:** run against one company's saved QuickBooks responses (QuickBooks Enterprise, qbXML 16.0, many years of history, calendar fiscal year, accrual basis). The rebuilt archive matched an archive built from the same responses by the original scripts, row for row.
- **Extractor:** run end to end against a simulated QuickBooks in the test suite (`tests/test_extract.py`). It descends from the extractor used on the real file, but parts were rewritten after a review and haven't been sent to a live QuickBooks since.
- **Direct connection (`com`) and remote bridge (`bridge/windows/`):** not run against QuickBooks in their current form. The bridge scripts pass a syntax check only.

## Open items, most likely to matter first

1. **Request shapes QuickBooks hasn't seen yet:** QuickBooks validates every request against its schema and may reject some of these:
   - the per-type count requests (`metaData="MetaDataOnly"`), especially on the item, terms, sales tax code and payment method lists
   - `IncludeLinkedTxns` on sales receipts, checks, card charges and journal entries
   - the sales tax code and payment method list queries

   A rejected count is recorded and fails `completeness` with the reason. A rejected document query stops the extractor with "parsing the provided XML". Either way nothing partial is loaded. The fix is usually in `qbd_archive/qbxml.py`: drop or move the element for that query, guided by Intuit's qbXML reference for your QuickBooks version.

2. **The month-by-month roll-forward view (`v_account_month`):** Each month's opening is built from the fiscal year's opening plus the activity since. The `month_continuity` check confirms the method against every beginning balance QuickBooks printed, but the view's own month-end figures haven't been compared with QuickBooks' reports on a real file. Before relying on it for month-end balances or a migration, compare a few month-ends with QuickBooks' balance sheet.

3. **Account numbers in report labels:** With QuickBooks' "use account numbers" preference on, reports may label accounts as "40000 · Sales". If so, ledger rows won't match the chart of accounts, and `accounts_placed`, `pnl_net_income` and `tieout` will fail broadly. Turn the preference off in the company copy before extracting, or teach `qbd_archive/gl.py` and the trial balance loader to strip the number.

4. **Slow ledger months aren't split:** Document windows split automatically when they time out. Ledger months don't, because each month's beginning balances anchor the checks. A month that can't finish within `report_timeout` needs a longer timeout.

5. **Error wording:** The extractor recognizes a revoked application grant by QuickBooks' message text, which hasn't been checked against every version. If the wording differs, the run still stops after `max_consecutive_failures` failures in a row, a few minutes later.

6. **Configurations never run:** QuickBooks Pro and Premier (exclude `transferinventory`, which only Enterprise has), non-calendar fiscal years, cash basis, multi-currency files, and QuickBooks versions older than the one tested.

7. **Deletion logs:** The deletion-log requests list the common document and list types, not every type the tool extracts, and they're saved as raw responses without being loaded into tables.

8. **Not extracted at all:** payroll documents (paychecks and liability checks; their ledger postings are in the archive), the audit trail, memorized transactions and attachments.

## Where things are decided

| Concern | File |
|---|---|
| What gets requested, and element order | `qbd_archive/qbxml.py` |
| Retries, timeouts, window splitting, locking | `qbd_archive/extract.py` |
| How QuickBooks is reached | `qbd_archive/transport.py`, `bridge/windows/` |
| How the ledger report is read and rows placed | `qbd_archive/gl.py` |
| Which fields become columns, document totals | `qbd_archive/load.py` |
| The checks | `qbd_archive/validate.py` |
| Views | `qbd_archive/views.sql` |
