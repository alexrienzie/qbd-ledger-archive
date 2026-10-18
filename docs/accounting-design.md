# Accounting design

This tool was designed by people who would have to defend its numbers. The standard we held it to: a controller, a tax preparer or an auditor should be able to pull a figure from the archive, trace it to its source, and reproduce it from QuickBooks' own reports. This page explains how the design gets there.

## The posture: an archive of the books as kept

The archive records the books exactly as QuickBooks held them. It doesn't correct misclassifications, re-derive postings from source documents or restate anything. If the books contain errors (and old books usually do), you want an untouched record of what they said, so any correction is a separate, documented adjustment on top of it.

Three layers, each with a different job:

| Layer | Contents | Role |
|---|---|---|
| Raw corpus (`raw/`) | Every qbXML response QuickBooks returned, verbatim, with a SHA-256 hash in the manifest | The source of record. Never edited. |
| Database (`company.sqlite`) | Tables parsed from the raw corpus, rebuilt from scratch on every load | The working copy. Disposable and reproducible. |
| Validation (`validation/`) | The check results (`summary.md`, `status.json`) and CSVs for drill-down | The evidence that the database matches the books. |

Ledger rows, beginning balances, report rows, documents and list records carry `src_key` (the raw response they came from), and ledger rows also carry `src_row`, so any of those figures can be traced to the response file and row it came from. Lines and links belong to a document, which carries the key.

## Which numbers are authoritative

- **The general ledger detail report is the posting record:** QuickBooks' own ledger is the authority for what posted where. The archive never rebuilds postings from invoices or bills, because QuickBooks applies posting logic (item accounts, sales tax, inventory cost relief, discounts) that documents don't fully carry.
- **QuickBooks' rendered trial balance, P&L and balance sheet are the benchmarks:** They're stored as reported, so the archive can be checked against them and so you have the statements as the books presented them, which is often what a dispute or a review needs.
- **Source documents are the detail behind the ledger:** They answer who, what item, which invoice a payment settled, and what the customer was billed. They join to the ledger on QuickBooks' transaction number.

## Basis

Accrual is the books-of-record basis, and every check ties on it. The basis is stated explicitly on every report request. Without it, a report follows the company's report preference (as we understand QuickBooks' behavior), and a cash-basis ledger compared against an accrual trial balance produces differences that look like missing data. The original build lost hours re-pulling "corrupted" months before finding that the two sides were on different bases. Cash basis can be pulled alongside (`bases = ["accrual", "cash"]`) and ties to a cash-basis trial balance on its own; that path hasn't been run on a real file yet.

## The period plan

The configuration produces a plan: every report chunk the archive needs, each with its dates. Extraction pulls exactly the plan, the loader loads exactly the plan, and the `plan_coverage` check fails if anything is missing, doubled or out of place. Chunk names include their dates, so changing the configuration produces new chunks and old ones are never reused.

Every chunk belongs to exactly one period, and every period has its own trial balance:

- **pre:** everything before `books.start`, in one chunk
- **FY2017, FY2018, ...:** one period per fiscal year, with the ledger pulled month by month
- **post:** everything after `books.end`, in one chunk

Set `books.start` to the first day of the first fiscal year with any activity. In the file this was built on, a ledger request spanning several fiscal years showed income and expense detail only for the last year and folded earlier years into "Closing Entry" lines against retained earnings, and the trial balance behaved the same way, so a tie-out alone didn't reveal it. The `fiscal_year_detail` check fails on any nonzero closing entry, which is the signal to move `books.start` earlier.

The catch-alls are a completeness control: opening entries dated years before the real start, or stray entries after the last close, land in a period with its own trial balance, so nothing falls outside the archive. Document queries use the same catch-all windows at both ends.

Fiscal years follow `books.fiscal_year_start_month`, which has to match the company file's own setting (the `fiscal_year_setting` check compares them), and `books.start` must be the first day of a fiscal year.

## The checks, as assertions

| Check | Assertion | How it's checked |
|---|---|---|
| `plan_coverage` | Nothing missing from the timeline | Every planned report chunk is loaded with its planned dates, ledger chunks run day after day with no gap or overlap, and every document type finished extracting. |
| `tieout` | Completeness and accuracy of the ledger | For each period and account: QuickBooks' beginning balance + debits - credits = QuickBooks' trial balance, to the cent. An account on only one side ties only if the other side is zero. |
| `month_continuity` | Accuracy within the year | Each month's beginning balance (as QuickBooks printed it) equals the prior month's ending balance, per account. A misplaced row inside a year breaks this even when the year ties. |
| `fiscal_year_detail` | Completeness of P&L detail | No ledger chunk contains nonzero "Closing Entry" lines. |
| `pnl_net_income` | Classification and P&L completeness | Net income built from ledger activity, grouped by account type, equals net income on QuickBooks' P&L. A ledger that ties but carries an account under the wrong type fails here, and so does P&L detail missing from a period (this check found the collapsed-year behavior above). A P&L with no net income line fails. |
| `fiscal_year_setting` | The periods are the company's periods | The configured fiscal year start matches the company file. |
| `ledger_balanced` | Integrity of each pull | Debits equal credits in every chunk. A truncated response fails. |
| `accounts_placed` | Every posting is on an account | No ledger row is left without an account. The rule that placed each row is counted (see below). |
| `completeness` | Every document and list record was extracted | For each type, records loaded equal QuickBooks' own count. A variance passes only when byte-identical copies inside a single response explain all of it. Hard gaps fail, and so does a type QuickBooks counted but the archive didn't extract. |
| `line_detail` | Documents carry their detail | Documents with an amount have line items. |
| `no_conflicts` | No ambiguity in the record | No record came back twice with different content. |
| `lists_clean` | Names are reliable join keys | Full names are unique within each list, and flat lists use their own name. |
| `crosswalk` (warning) | Ledger and documents agree | Ledger transactions without a source document, and posting documents without ledger lines, grouped by type. Residue is expected for types this tool doesn't extract (payroll, for example), and the grouping shows which. |
| `linked_txns` (warning) | Application detail present | Document types extracted without linked-transaction detail. |

The validator exits non-zero if any check fails, so it can sit at the end of a script. Warnings don't fail the run but belong in your review. Treat any failure as a stop-work signal for whatever change preceded it.

## How ledger rows are placed on accounts

The ledger report encodes the account tree by position: section headers, rows, subtotals. In some company files, stretches of the report omit section headers, so rows arrive with no open section, or under a parent when the child's header is missing. Dropping those rows or guessing would both break the tie. Instead every row is also requested with its Account column, and the parser places each row by the first rule that settles it uniquely:

| Rule (`account_src`) | When it applies |
|---|---|
| `section` | The normal case: the row sits inside its account's section, and the Account column agrees |
| `section_child` | The row is under a parent, but its Account column names a child of that parent |
| `account_column` | No open section, and the Account column names exactly one account |
| `subtotal_cascade` | The short name is shared by several accounts; the subtotals that follow name exactly one candidate's parent |
| `subtotal_proximity` | Still ambiguous; the first parent subtotal after the row's own subtotal decides, if it points to exactly one candidate |
| `section_conflict` | The Account column names an account outside the open section. The row stays unplaced and validation fails. |
| `unresolved` | None of the above. The row stays unplaced and validation fails. |

In the file this was built on, a small fraction of ledger rows needed one of the fallback rules, all in one stretch of a few years, and all were placed. The `tieout`, `month_continuity` and `pnl_net_income` checks then confirm the placement from three directions.

## Duplicates and conflicts

The SDK sometimes returns the same record more than once. In the file this was built on, assembly builds were the case: some builds came back twice inside the same response, apparently once for each sub-assembly they consumed, so QuickBooks' own count for the type ran higher than the number of distinct builds. The loader keeps one copy of each record and logs every extra copy (`load_duplicate`), noting whether it was byte-identical and whether it came back in the same response. The completeness check accepts a count variance only when identical copies inside a single response explain all of it. A copy that turns up in two different responses doesn't count, because that's the pattern overlapping windows or a lost record would also produce.

## Cutover to a new system

If the company moved to QuickBooks Online or another system, the conversion usually posts opening or summary journal entries in the new books that restate the old ones. When you combine the two archives, pick a cutover date and keep detail from the old books before it and from the new books after it, excluding the conversion entries. Including both double-counts the overlap. Statement-level figures (a monthly P&L, for example) are easiest taken whole from one system per month.

## What the archive doesn't do

- **It doesn't correct the books:** misclassifications and errors are preserved.
- **Payroll documents:** paychecks and payroll liability checks aren't extracted as documents. Their ledger postings are in the archive.
- **Audit trail, memorized transactions and attachments:** not extracted. QuickBooks' deletion logs (which cover recent deletions only) are saved in the raw corpus as `single_txndeleted` and `single_listdeleted` but not loaded into tables.

## Terms

- **chunk:** one saved QuickBooks response (one month of ledger, one year's trial balance, one window of invoices)
- **catch-all:** the single chunk covering everything before `books.start` or after `books.end`
- **period:** a fiscal year, or one of the catch-alls; each has its own trial balance
- **probe:** a request that asks QuickBooks only for the number of records of a type
- **basis:** accrual or cash, the accounting basis a report is run on
- **transport:** how requests reach QuickBooks: directly on the QuickBooks machine (`com`) or through another program such as the remote bridge (`command`)
