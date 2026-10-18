# qbd-ledger-archive

Turn a QuickBooks Desktop company file into a SQLite archive you can rely on for financial reporting, tax work, audit support and a system migration. The archive is checked against QuickBooks' own trial balance, account by account and period by period, to the cent, and every number in it traces back to the response QuickBooks returned.

We built this when a company we work with moved off QuickBooks Desktop. Its history, many years of books with millions of ledger lines, needed a permanent home that didn't depend on a QuickBooks license, a Windows machine or a vendor's conversion tool. We came at it from the finance side: the test for every step was whether we'd sign off on numbers pulled from it. We built it with AI coding agents, and the hard parts were accounting judgment.

## What you get

- **The general ledger:** every posting line, on accrual basis (cash basis too if you want it), with QuickBooks' own beginning balances for every month.
- **QuickBooks' rendered reports:** trial balance, P&L and balance sheet for each fiscal year, stored as the books presented them.
- **Source documents with full line detail:** invoices, sales receipts, credit memos, payments and how they were applied, deposits, bills, bill payments, checks, card charges, journal entries, transfers, inventory adjustments and transfers, assembly builds, sales tax payments, statement charges, sales and purchase orders, and estimates.
- **Lists:** chart of accounts, customers and jobs, vendors, items with their income, COGS, asset and expense accounts, classes, terms and more.
- **A validation report** that says, check by check, whether the archive can be relied on, and exactly where it can't.

## Why it can be relied on

Most checks compare the archive to numbers QuickBooks reports about itself (its trial balance, its P&L, its own record counts); the rest check that the archive is internally consistent. The main ones:

| Check | What it proves |
|---|---|
| `tieout` | Beginning balance plus ledger activity equals QuickBooks' trial balance for every account in every period, on the same basis |
| `pnl_net_income` | Net income built from the ledger matches QuickBooks' P&L for every period |
| `plan_coverage` | Every period and month the archive should hold is there, with nothing missing or doubled |
| `month_continuity` | Each month opens where the prior month closed, account by account |
| `fiscal_year_detail` | No year's income and expense detail is hidden behind QuickBooks' year-end closing lines |
| `completeness` | Records loaded match QuickBooks' own count for every list and document type |
| `accounts_placed` | Every ledger row sits on an account, with the rule that placed it |
| `line_detail` | Documents that carry an amount carry their lines |

There are 14 checks in all; [docs/accounting-design.md](docs/accounting-design.md) explains each one from a controller's point of view, along with how the period plan keeps anything from falling between chunks and what the archive deliberately doesn't do.

## How it works

```
QuickBooks Desktop --qbXML--> extract --> raw/ (verbatim responses + manifest)
                                             |
                                   load (rebuilds from raw/, every time)
                                             |
                                    company.sqlite --> validate --> validation/
```

1. **Extract:** pulls everything through the QuickBooks SDK. Document queries that time out are split into smaller date windows until they finish, with line detail intact. Every response is saved verbatim with its hash, and a stopped run resumes where it left off.
2. **Load:** rebuilds the database from the saved responses. It never patches rows in place, so the database can always be regenerated from what QuickBooks returned.
3. **Validate:** runs the checks and writes a summary you can hand to a reviewer.

## Quick start

On the Windows machine where QuickBooks Desktop is installed, logged in as a user who can open the company file:

1. Install Python 3.11 or later from python.org (the Microsoft Store shortcut named `python` can get in the way; the `py` launcher avoids it).
2. Download this repository (Code > Download ZIP, or `git clone`) and open PowerShell in its folder.
3. Install it with the QuickBooks connector:

   ```powershell
   py -m pip install -e ".[com]"
   ```

4. Copy `archive.example.toml` to `archive.toml` and set the company file path and the books' start and end dates.
5. Run it:

   ```powershell
   qbd-archive run --config archive.toml
   ```

On the first request QuickBooks shows its application certificate dialog, which you approve once, logged in as the QuickBooks Admin in single-user mode, and allow access even when QuickBooks isn't running, so later runs can proceed unattended (the exact wording varies by QuickBooks version). Leave the box for personal data (social security and credit card numbers) unchecked.

Work from a copy of the company file. [docs/running-an-extraction.md](docs/running-an-extraction.md) covers setup, timing, what stalls QuickBooks, how to recover, and what to do with each validation result.

## Documentation

- [docs/accounting-design.md](docs/accounting-design.md): the design, from the accounting side
- [docs/running-an-extraction.md](docs/running-an-extraction.md): setup, timing, recovery and reading the validation results
- [docs/lessons.md](docs/lessons.md): what went wrong in the original build, and the rule each problem produced
- [docs/using-the-data.md](docs/using-the-data.md): queries for reporting, tax, receivables, inventory and migration work
- [docs/schema.md](docs/schema.md): tables, views and terms
- [docs/known-limitations.md](docs/known-limitations.md): what hasn't been proven yet, and where to look when a new file doesn't cooperate
- [bridge/windows/README.md](bridge/windows/README.md): running extraction from another machine
- [SECURITY.md](SECURITY.md): keeping company data out of places it shouldn't go

## Requirements

Python 3.11 or later, standard library only, plus `pywin32` on the QuickBooks machine for the direct connection. QuickBooks Desktop with the SDK's request processor, which installs with QuickBooks. Developed against QuickBooks Enterprise with qbXML 16.0. On Pro or Premier, inventory transfers don't exist; add `exclude_txn_types = ["transferinventory"]` to the config.

To run the tests (they use made-up data and don't need QuickBooks):

```powershell
py -m unittest discover -s tests -t .
```

## Status

The loader, parsers and checks were verified by rebuilding an archive from one company's saved QuickBooks responses (a mature Enterprise file, many years of history, calendar fiscal year, accrual basis) and comparing it with an archive built from the same responses by the original scripts: every ledger row, beginning balance, list record and document matched, and every period tied. That comparison also exposed two gaps in the original build, which the checks now catch: income and expense detail hidden inside a multi-year catch-all chunk, and two document types that hadn't been extracted.

The extraction engine is tested against a simulated QuickBooks. The direct connection and the remote bridge haven't been run against a live QuickBooks in this form. Also untested: Pro and Premier, non-calendar fiscal years, multi-currency files and cash basis. [docs/known-limitations.md](docs/known-limitations.md) lists every open item and where in the code it'd be fixed. Reports from anyone who runs it are welcome (without company data; see [SECURITY.md](SECURITY.md)).

## License

MIT. See [LICENSE](LICENSE).
