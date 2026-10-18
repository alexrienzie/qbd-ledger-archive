# Schema

The authority is [`schema.sql`](../qbd_archive/schema.sql) and [`views.sql`](../qbd_archive/views.sql). Conventions everywhere: amounts are integer cents in `*_c` columns; dates are ISO `YYYY-MM-DD`; QuickBooks' `ListID`, `TxnID` and `TxnLineID` are kept verbatim; `src_key` names the raw response a row came from (`raw/<src_key>.xml`). Terms such as chunk, period and probe are defined at the end of [accounting-design.md](accounting-design.md).

## Views (the query surface)

| View | One row per | Notes |
|---|---|---|
| `v_gl` | accrual ledger posting line | `signed_c` = debit - credit; `account_src` = placement rule |
| `v_gl_cash` | cash-basis ledger line | only if cash basis was extracted |
| `v_account_month` | account per ledger chunk | QuickBooks' beginning balance, debits, credits, net, ending balance; months labeled `YYYY-MM`, catch-alls `pre` and `post` |
| `v_trial_balance` | account per period and basis | QuickBooks' rendered trial balance |
| `v_pnl`, `v_balance_sheet` | statement row | QuickBooks' rendered statements, in report order (`seq`) |
| `v_account` | account | type, number, parent; `statement` = `pnl` or `bs` |
| `v_customer`, `v_item` | list record | items carry their income, COGS, asset and expense accounts |
| `v_vendor` | vendor | `eligible_1099` from the vendor's 1099 flag |
| `v_txn` | source document | header, entity, account, document total (`amount_c`), pending flag, and sales tax and ship-to fields |
| `v_txn_line` | document line | item, quantity, rate, amount, with an account on every line that can be grounded (`account_src`); `line_sign` is -1 on credit memos, vendor and card credits, and journal credit lines |
| `v_txn_link` | link between documents | `applied` (payment to invoice), `deposited` (deposit to payment), `refund_applied`, and QuickBooks' own link types |
| `v_item_movement` | inventory quantity movement | inventory and assembly items; pending documents excluded |
| `xwalk_orphans` | unmatched transaction | ledger without document, or document without ledger |

`amount_c` on `v_txn` is the document total as QuickBooks states it: subtotal plus sales tax for invoices, the credit amount for vendor credits, the sum of debit lines for journal entries, and no total for assembly builds, inventory adjustments and inventory transfers.

## Tables

| Table | Holds |
|---|---|
| `gl_line` | every ledger posting row, both bases (`basis` column) |
| `gl_beginning_balance` | per account, the beginning balance QuickBooks printed at the start of each chunk |
| `rpt_tb` | rendered trial balances |
| `rpt_summary` | rendered P&L and balance sheet rows (`kind` = `pnl` or `bs`) |
| `dim_list` | every list record, with its full XML |
| `dim_account` | chart of accounts, from `dim_list` |
| `map_item_account` | item to account references by role |
| `fact_txn` | document headers |
| `fact_txn_line` | document lines; `line_id_synthetic` = 1 where QuickBooks gave no TxnLineID |
| `fact_txn_link` | document-to-document links |
| `load_duplicate` | every extra copy the SDK returned: whether it was byte-identical and whether it came back in the same response |
| `probe_count` | QuickBooks' own record counts per type |
| `extract_type` | per document type: extraction status (`complete`, `gaps`, `unsupported`), windows, gaps, linked-transaction flag |
| `source_file` | one row per raw response in the current plan: kind, basis, period, dates, SHA-256, size |
| `meta` | tool version, load time, configuration, company name and fiscal year setting |

## Periods

`period` values are `pre`, `FY<year>` (labeled by the calendar year the fiscal year ends in) and `post`. Ledger chunks are monthly within fiscal years; `pre` and `post` are single catch-all chunks.

## Line tags

`fact_txn_line.line_tag` keeps QuickBooks' element name, which tells you what the line is: `InvoiceLineRet`, `ItemLineRet` and `ExpenseLineRet` (bills, checks, card charges), `DepositLineRet`, `JournalDebitLine` / `JournalCreditLine`, `InventoryAdjustmentLineRet` (quantity and value are the changes), `ComponentItemLineRet` and the synthetic `AssemblyOutput` (assembly builds), `SalesTaxPaymentCheckLineRet`, `TransferInventoryLineRet`, and `ChargeRet` for statement charges.
