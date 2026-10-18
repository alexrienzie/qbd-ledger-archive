# Using the data

Open the archive with any SQLite client (`sqlite3 company.sqlite`, DB Browser for SQLite, Python, R, or a BI tool through an ODBC driver). Query the `v_*` views; the tables behind them are extraction detail.

Amounts are integer cents. Divide by 100.0 for dollars. `signed_c` is debit minus credit, so income and liabilities come out negative.

## Citing a figure

Every ledger row carries `src_key` and `src_row`, and every document and list record carries `src_key`. To cite a number, give the view, the filter, and the period, and keep `validation/summary.md` with it as evidence that the archive ties. To show the raw record behind a row, open `raw/<src_key>.xml`; `src_row` counts data rows in document order.

## Reporting

**Trial balance as QuickBooks reported it, for a fiscal year:**

```sql
SELECT account_fullname, debit_c/100.0 AS debit, credit_c/100.0 AS credit
FROM v_trial_balance WHERE basis='accrual' AND period='FY2023'
ORDER BY account_fullname;
```

**Monthly P&L by account, from the ledger:**

```sql
SELECT m.month, m.account_fullname, -m.net_c/100.0 AS amount
FROM v_account_month m JOIN v_account a ON a.fullname = m.account_fullname
WHERE a.statement='pnl' AND m.month BETWEEN '2023-01' AND '2023-12'
ORDER BY m.account_fullname, m.month;
```

(Income shows positive and expenses negative; flip the sign for expense lines if you prefer.)

**Month-end balance sheet balances:**

```sql
SELECT m.account_fullname, m.end_c/100.0 AS balance
FROM v_account_month m JOIN v_account a ON a.fullname = m.account_fullname
WHERE a.statement='bs' AND m.month='2023-12'
ORDER BY m.account_fullname;
```

`begin_c` is the beginning balance QuickBooks itself printed for the month, so month-end balances don't depend on summing history, and the `month_continuity` check confirms each month opens where the last one closed. Months run from `books.start` to `books.end`; activity before and after sits in single rows labeled `pre` and `post`. Retained earnings and current-year earnings follow QuickBooks' own year-end treatment: current-year income sits in the P&L accounts until the year closes.

**The statements as the books presented them:**

```sql
SELECT row_type, label, amount_c/100.0 FROM v_pnl
WHERE basis='accrual' AND period='FY2023' ORDER BY seq;
```

## Tax

**Sales by ship-to state, for nexus and sales tax work:** sales documents carry their ship-to and bill-to state, subtotal, sales tax charged, tax rate, tax item and the customer's tax code:

```sql
SELECT substr(txn_date,1,4) AS year, COALESCE(ship_state, bill_state) AS state,
       COUNT(*) AS documents,
       SUM(CASE WHEN txn_kind='creditmemo' THEN -subtotal_c ELSE subtotal_c END)/100.0 AS net_sales,
       SUM(CASE WHEN txn_kind='creditmemo' THEN -sales_tax_c ELSE sales_tax_c END)/100.0 AS tax_charged
FROM v_txn
WHERE txn_kind IN ('invoice','salesreceipt','creditmemo')
  AND COALESCE(is_pending,'false') <> 'true'
GROUP BY 1, 2 ORDER BY 1, 2;
```

Check how the company recorded addresses before relying on state: some files put the state only on the bill-to address, and marketplace or web sales are often booked in bulk to one customer without a ship-to address.

**1099 review, payments to vendors for a calendar year:**

```sql
SELECT t.entity_name, v.eligible_1099, COUNT(*) AS payments, SUM(t.amount_c)/100.0 AS total
FROM v_txn t JOIN v_vendor v ON v.id = t.entity_id
WHERE t.txn_kind IN ('check','billpayck','billpaycc','cccharge')
  AND t.txn_date BETWEEN '2023-01-01' AND '2023-12-31'
GROUP BY 1, 2 ORDER BY total DESC;
```

The join to `v_vendor` drops payments to customers, employees and other names; `eligible_1099` is the vendor's 1099 flag in QuickBooks. Treat it as a starting list for review: amounts include every line of each payment, including reimbursed expenses and goods. Card payments (`billpaycc`, `cccharge`) are generally reported by the card processor on Form 1099-K; check the current IRS instructions for Forms 1099-MISC and 1099-NEC with your tax adviser.

**Book-to-return reconciliation:** the trial balance for the fiscal year is the starting point; adjustments belong outside the archive.

## Receivables

**What a payment settled:**

```sql
SELECT p.txn_date, p.doc_number, l.linked_txn_id, i.doc_number AS invoice, l.amount_c/100.0
FROM v_txn p
JOIN v_txn_link l ON l.txn_id = p.txn_id AND l.link_kind = 'applied'
JOIN v_txn i ON i.txn_id = l.linked_txn_id
WHERE p.txn_kind = 'recvpayment' AND p.entity_name = 'Customer Name';
```

Payment applications always come through. Credit-memo applications come through only when the extraction requested linked transactions and QuickBooks accepted (see the `linked_txns` check). When rebuilding a customer's open balance from documents, include refunds as well as payments and credit memos, or a refunded overpayment reads as a customer credit. For an as-of open balance, QuickBooks' own A/R aging report as of that date is the authority; documents explain it.

## Inventory

`v_item_movement` gives signed quantity movements per inventory and assembly item: purchases, sales, returns, adjustments and assembly builds (output in, components out). Pending documents are left out. Moves between inventory sites (Enterprise) don't change total quantity and aren't included.

```sql
SELECT item_name, SUM(qty_delta) AS on_hand
FROM v_item_movement WHERE txn_date <= '2023-12-31'
GROUP BY 1 HAVING ABS(SUM(qty_delta)) > 0 ORDER BY 1;
```

Inventory value lives in the ledger: QuickBooks relieves inventory at cost when it posts a sale (average cost by default; Enterprise's advanced inventory can use FIFO). Use the inventory asset accounts in `v_account_month` for value, and the movement view for quantities.

## Migration to another system

Most accounting systems import history as opening balances plus monthly summary journal entries. `v_account_month` is that shape: one row per account per month, with the beginning balance, debits, credits and ending balance (the `pre` row's ending balance is the opening position at `books.start`). Pair it with the lists (`v_account`, `v_customer`, `v_vendor`, `v_item`) for master data, and open documents (unpaid invoices and bills as of cutover) from QuickBooks' own aging reports.

## Tracing a ledger line to its document

Ledger rows and documents share QuickBooks' transaction number:

```sql
SELECT g.txn_date, g.account_fullname, g.signed_c/100.0, t.txn_kind, t.doc_number, t.entity_name
FROM v_gl g LEFT JOIN v_txn t ON t.txn_number = g.txn_number
WHERE g.txn_number = '12345';
```

`xwalk_orphans` lists what doesn't join in either direction.
