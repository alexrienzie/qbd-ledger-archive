# Lessons from the original build

Each of these cost real time or produced a wrong interim number when we archived a large company file. Each one is now a rule in the code. They describe what we saw in that file, on QuickBooks Enterprise; other editions and versions may behave differently.

## Accounting

**Check which side is wrong, and whether the bases match, before re-pulling anything:** Three years of our ledger "didn't tie," and we re-pulled them as corrupted. The ledger had come back on cash basis (the company's report preference) while the trial balance was accrual. Basis is now explicit on every request, and the tie only compares like with like.

**A ledger request that spans fiscal years hides the earlier years' P&L detail:** In our file, for a multi-year range, QuickBooks' general ledger showed income and expense postings only for the last fiscal year; earlier years collapsed into "Closing Entry" lines against retained earnings. Its trial balance "as of" the same date had the same blind spot, so the tie-out passed anyway. We found it only when the net-income check compared the ledger to QuickBooks' P&L for the catch-all period before our first full year: an invoice and a bill from the company's first months were missing as line detail, visible only as a small closing entry. The archive now fails validation on any nonzero closing entry, and the fix is to start `books.start` at the first fiscal year with activity. A ledger that ties to the trial balance and still misses P&L detail is the reason to check net income against QuickBooks' own P&L for every period, catch-alls included.

**A matching total isn't a tie:** The tie-out works account by account, period by period, to the cent. A period that ties in total can still hide offsetting errors between accounts.

**Never drop detail to make a query finish:** When a query was too heavy, the tempting fix was to pull headers without line items. That trades a slow archive for an unreliable one. The extractor shrinks the date window instead, down to a single day, and records anything still unpullable as a gap that fails validation.

**A count variance isn't always lost data:** QuickBooks' own count of assembly builds ran several hundred above the number we loaded. The difference appeared to be byte-identical re-emissions, inside the same response, of builds that consume sub-assemblies. The loader now logs every duplicate, and the completeness check accepts a variance only when identical copies inside a single response explain all of it; a copy that turns up in two responses could just as well be an overlap or a lost record.

**Some fields live in type-specific elements:** Inventory adjustment lines carry their changes as `QuantityDifference` and `ValueDifference`; a generic line parser loaded all of them blank. Assembly builds carry components as `ComponentItemLineRet` with `QuantityNeeded`. Sales tax payments reference `ItemSalesTaxRef`. Each has its own mapping now.

**Document totals live in different fields:** Invoices carry no total at all (subtotal plus sales tax make it), and vendor credits keep theirs in `CreditAmount`. A generic "first amount field" rule loaded every vendor credit with no amount and every invoice before tax. Each type now names its own total field.

**Ask for linked transactions up front:** Payments carry the invoices they applied to, but credit-memo applications came back only with `IncludeLinkedTxns`. We had to go back for them months later to reconstruct customer balances.

**Old books carry two systems' worth of history at the cutover:** A conversion to a new system typically posts summary entries that restate the old books. Combine the two at one cutover date and exclude the conversion entries, or the overlap counts twice.

## Parsing

**Never take the first `<FullName>` in a record:** Nested references carry full names too. A vendor's `VendorTypeRef` names its category; reading the first full name renamed vendors after their category. Flat lists (vendors, terms, employees and others) have no full name of their own, so their name is their identity.

**The ledger report's grammar has exceptions:** An aggregate parent header carries its children's total, which isn't a beginning balance. Direct postings to a parent with children sit under "Parent - Other." Retained earnings closes with a data row instead of a subtotal. And in some files, stretches of the report omit section headers entirely. [accounting-design.md](accounting-design.md) describes how each row is placed and how the rule is recorded.

**Find columns by title:** Column positions vary by request and version; titles don't.

## Talking to QuickBooks

**The SDK only worked in a logged-on desktop session:** From a service or an SSH session, QuickBooks wouldn't start. To drive it remotely, run the worker as a scheduled task in the interactive session (see `bridge/windows/`).

**A modal dialog on QuickBooks' screen stops everything:** QuickBooks has one UI thread. A timed popup after hours of uptime, or the startup screen when QuickBooks launches with the machine, stalls every request until someone clicks it. Requests get timeouts, and a run of consecutive failures stops the extractor for a person to look.

**Don't force-quit QuickBooks:** Repeated kills in a few hours appeared to trip a QuickBooks integrated-application check that revoked the unattended-access grant (fixable only in the GUI), and on a hosted file it left ghost sessions in the database server that locked the file for every user until the services were restarted. Wait out stalls instead.

**Work from a copy of the company file:** Extracting against a hosted file in daily use brings in the database server, other users' locks and any web connectors that sync on a schedule. A verified copy (compare SHA-256 hashes) removes all of that, and doubles as a byte-level archive of the file itself.

**Iterators don't survive a session per request:** qbXML iterators are tied to one SDK session. A worker that opens a fresh session for each request (which is what makes a stalled request safe to abandon) breaks every iterator. Date windows work with any transport.

**Window size follows cross-references:** A payment that applies to dozens of invoices is far more expensive for QuickBooks to materialize than an invoice with its own lines. Payments, bill payments and deposits start with monthly windows; invoices, sales orders and assembly builds start monthly for volume; card charges, credit memos and card refunds start quarterly; everything else starts at a year. The extractor splits any document window that still times out.

**A schema error isn't a size problem:** If QuickBooks rejects the request XML itself, shrinking the window just repeats the failure thousands of times (we lost a batch run to that with assembly builds, which need `IncludeComponentLineItems` instead of `IncludeLineItems`). The extractor stops on schema errors and says so.

**Element order matters:** On the ledger report, `ReportBasis` had to follow the `IncludeColumn` list; placed before it, our version of QuickBooks rejected the request.

**Pin the qbXML version:** The request processor reported a newer version than our QuickBooks accepted. 16.0 worked for us and covers everything here; older QuickBooks releases may need an older version.

**Ask for counts one type at a time:** One unsupported query in a batch with `stopOnError` cancelled every query after it, and a request QuickBooks can't parse fails whole even with `continueOnError`. Each type now gets its own count request, so an edition without a type only loses that type's count.

## Running long jobs

**Supervise from outside:** Our first overnight run hung silently for most of the night; in-process retry logic can't see its own hang. Log every line with a timestamp, keep a heartbeat (the extractor touches its lock file every 30 seconds), and have something external restart the run when the log goes stale. The manifest makes restarts free.

**One driver per archive, and per bridge:** Two sessions once worked the same bridge at the same time and one archived the other's response under its own name. The extractor holds a lock on the archive, and every response is checked against its own request (a unique request ID, the response type, the report title, dates inside the window) before it's accepted.

**Write a completion marker only when everything is verified:** `EXTRACT_COMPLETE` appears only when every planned chunk is in the manifest and every document type finished without gaps.
