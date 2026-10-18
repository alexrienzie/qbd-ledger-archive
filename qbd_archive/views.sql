-- Query surface. Use these views; the tables behind them are extraction
-- detail and reconciliation anchors.
--
-- Sign convention: signed_c = debit - credit. Assets and expenses are
-- positive; liabilities, equity and income are negative.
-- v_gl is the accrual ledger, the books-of-record basis the archive ties on.

CREATE VIEW v_account AS
SELECT list_id AS id, name, fullname, account_type, special_account_type,
       account_number, parent_listid AS parent_id, is_active,
       CASE WHEN account_type IN ('Income','OtherIncome','CostOfGoodsSold',
                                  'Expense','OtherExpense')
            THEN 'pnl' ELSE 'bs' END AS statement
FROM dim_account;

CREATE VIEW v_customer AS
SELECT list_id AS id, name, fullname, parent_listid AS parent_id, is_active
FROM dim_list WHERE list_type='customer';

CREATE VIEW v_vendor AS
SELECT list_id AS id, name, fullname, is_active,
       CASE WHEN instr(xml, '<IsVendorEligibleFor1099>true</IsVendorEligibleFor1099>') > 0
            THEN 1 ELSE 0 END AS eligible_1099
FROM dim_list WHERE list_type='vendor';

CREATE VIEW v_item AS
SELECT d.list_id AS id, d.name, d.fullname,
       REPLACE(REPLACE(d.record_tag,'Item',''),'Ret','') AS item_type,
       d.parent_listid AS parent_id, d.is_active,
       m.income_fullname, m.cogs_fullname, m.asset_fullname,
       m.expense_fullname, m.generic_fullname
FROM dim_list d LEFT JOIN map_item_account m ON m.item_listid = d.list_id
WHERE d.list_type='item';

CREATE VIEW v_gl AS
SELECT txn_date, txn_type, txn_number, ref_number AS doc_number,
       name AS entity_name, memo, account_fullname, account_src,
       COALESCE(debit_c,0) AS debit_c, COALESCE(credit_c,0) AS credit_c,
       COALESCE(debit_c,0) - COALESCE(credit_c,0) AS signed_c, src_key, src_row
FROM gl_line WHERE basis='accrual';

CREATE VIEW v_gl_cash AS
SELECT txn_date, txn_type, txn_number, ref_number AS doc_number,
       name AS entity_name, memo, account_fullname, account_src,
       COALESCE(debit_c,0) AS debit_c, COALESCE(credit_c,0) AS credit_c,
       COALESCE(debit_c,0) - COALESCE(credit_c,0) AS signed_c, src_key, src_row
FROM gl_line WHERE basis='cash';

-- Roll-forward per account per ledger chunk (accrual). Fiscal-year chunks
-- are calendar months (label YYYY-MM); the catch-alls before books.start and
-- after books.end are one row each (label 'pre' / 'post').
-- begin_c is the period's opening balance as QuickBooks printed it at the
-- start of the fiscal year (or catch-all), plus the activity of the earlier
-- months in that period. That keeps it right even in months where QuickBooks
-- left an account's beginning balance off the report; the month_continuity
-- check compares it with every beginning balance QuickBooks did print.
-- Opening balances plus these monthly rows are the shape most accounting
-- systems import as history.
CREATE VIEW v_account_month AS
WITH chunk AS (
  SELECT src_key, period, period_from, period_to,
         CASE WHEN period IN ('pre','post') THEN period
              ELSE substr(period_from,1,7) END AS month
  FROM source_file WHERE kind='gl' AND basis='accrual'),
first_chunk AS (
  SELECT c.period, c.src_key FROM chunk c
  WHERE c.period_from = (SELECT MIN(period_from) FROM chunk c2 WHERE c2.period = c.period)),
opening AS (
  SELECT f.period, b.account_fullname, SUM(b.balance_c) AS open_c
  FROM gl_beginning_balance b JOIN first_chunk f ON f.src_key = b.src_key
  WHERE b.basis='accrual' GROUP BY 1, 2),
act AS (
  SELECT src_key, account_fullname,
         SUM(COALESCE(debit_c,0)) AS debit_c, SUM(COALESCE(credit_c,0)) AS credit_c
  FROM gl_line WHERE basis='accrual' GROUP BY 1, 2),
accounts AS (
  SELECT period, account_fullname FROM opening
  UNION SELECT c.period, a.account_fullname FROM act a JOIN chunk c ON c.src_key = a.src_key),
grid AS (
  SELECT c.*, x.account_fullname,
         COALESCE(a.debit_c,0) AS debit_c, COALESCE(a.credit_c,0) AS credit_c,
         COALESCE(o.open_c,0) AS open_c
  FROM chunk c JOIN accounts x ON x.period = c.period
  LEFT JOIN act a ON a.src_key = c.src_key AND a.account_fullname = x.account_fullname
  LEFT JOIN opening o ON o.period = c.period AND o.account_fullname = x.account_fullname),
rolled AS (
  SELECT *, open_c + COALESCE(SUM(debit_c - credit_c) OVER (
              PARTITION BY period, account_fullname ORDER BY period_from
              ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING), 0) AS begin_c
  FROM grid)
SELECT month, period, period_from, period_to, account_fullname, begin_c,
       debit_c, credit_c, debit_c - credit_c AS net_c,
       begin_c + debit_c - credit_c AS end_c
FROM rolled;

CREATE VIEW v_trial_balance AS
SELECT basis, period, period_from, period_to, account_fullname,
       debit_c, credit_c, debit_c - credit_c AS signed_c
FROM rpt_tb;

CREATE VIEW v_pnl AS
SELECT basis, period, seq, row_type, label, amount_c FROM rpt_summary WHERE kind='pnl';

CREATE VIEW v_balance_sheet AS
SELECT basis, period, seq, row_type, label, amount_c FROM rpt_summary WHERE kind='bs';

CREATE VIEW v_txn AS
SELECT txn_id, txn_kind, ret_tag,
       CASE WHEN txn_kind IN ('salesorder','purchaseorder','estimate') THEN 0 ELSE 1 END
         AS is_posting,
       txn_number, txn_date, ref_number AS doc_number,
       entity_listid AS entity_id, entity_fullname AS entity_name,
       account_listid AS account_id, account_fullname AS account_name,
       amount_c, memo, is_pending, time_created, time_modified,
       subtotal_c, sales_tax_c, sales_tax_pct, sales_tax_item, customer_tax_code,
       ship_state, ship_postal, ship_country, bill_state, src_key
FROM fact_txn;

-- Line detail with an account on every line where one can be grounded:
-- the line's own account, or else the item's account for the document's role
-- (income for sales documents, asset for inventory items on purchases,
-- expense otherwise). account_src says which.
CREATE VIEW v_txn_line AS
SELECT l.txn_id, l.txn_line_id AS line_id, l.line_id_synthetic, l.seq,
       t.txn_kind, t.txn_date, t.doc_number, t.entity_name,
       l.line_tag, l.item_listid AS item_id, l.item_fullname AS item_name,
       l.descr AS description, CAST(l.quantity AS REAL) AS quantity, l.rate, l.amount_c,
       COALESCE(NULLIF(l.account_fullname,''),
         CASE WHEN t.txn_kind IN ('invoice','creditmemo','salesreceipt','salesorder','estimate')
              THEN COALESCE(m.income_fullname, m.generic_fullname)
              WHEN m.item_record_tag IN ('ItemInventoryRet','ItemInventoryAssemblyRet')
              THEN m.asset_fullname
              ELSE COALESCE(m.expense_fullname, m.generic_fullname) END) AS account_name,
       CASE WHEN NULLIF(l.account_fullname,'') IS NOT NULL THEN 'line'
            WHEN m.item_listid IS NOT NULL THEN 'item' END AS account_src,
       l.class_fullname AS class_name, l.entity_fullname AS line_entity,
       -- line_sign: -1 where the line reverses its document family's usual
       -- direction (credit memos, vendor and card credits, journal credits)
       CASE WHEN t.txn_kind IN ('creditmemo','vendorcredit','cccredit')
              OR l.line_tag = 'JournalCreditLine' THEN -1 ELSE 1 END AS line_sign
FROM fact_txn_line l
JOIN v_txn t ON t.txn_id = l.txn_id
LEFT JOIN map_item_account m ON m.item_listid = l.item_listid;

CREATE VIEW v_txn_link AS
SELECT l.txn_id, t.txn_kind, t.txn_date, l.linked_txn_id, l.linked_txn_type,
       l.link_kind, l.amount_c
FROM fact_txn_link l JOIN fact_txn t ON t.txn_id = l.txn_id;

-- Ledger rows with no source document, and posting documents with no ledger
-- rows, joined on TxnNumber. Expected residue comes from document types not
-- extracted (payroll, for example); grouped by type in validation.
CREATE VIEW xwalk_orphans AS
SELECT 'gl_without_txn' AS side, g.txn_type, g.txn_number,
       MIN(g.txn_date) AS txn_date, COUNT(*) AS gl_lines,
       SUM(COALESCE(g.debit_c,0)) AS amount_c
FROM gl_line g LEFT JOIN fact_txn t ON t.txn_number = g.txn_number
WHERE g.basis='accrual' AND t.txn_number IS NULL
  AND g.txn_number IS NOT NULL AND g.txn_number <> ''
GROUP BY g.txn_type, g.txn_number
UNION ALL
SELECT 'txn_without_gl', t.txn_kind, t.txn_number, t.txn_date, 0, t.amount_c
FROM fact_txn t
LEFT JOIN (SELECT DISTINCT txn_number FROM gl_line WHERE basis='accrual') g
       ON g.txn_number = t.txn_number
WHERE g.txn_number IS NULL
  AND t.txn_kind NOT IN ('salesorder','purchaseorder','estimate')
  AND COALESCE(t.is_pending,'false') <> 'true';

-- Perpetual inventory movements for inventory and assembly items.
-- qty_delta: + into stock, - out. value_delta_c only where the document
-- itself states the value (purchase lines, adjustment value differences).
-- Sales relieve inventory at average cost inside QuickBooks' ledger, not on
-- the document line, so value on sales rows is NULL here and the ledger stays
-- the value of record.
CREATE VIEW v_item_movement AS
SELECT t.txn_kind, t.txn_id, l.txn_line_id, t.txn_date, t.ref_number AS doc_number,
       t.entity_fullname AS entity_name, l.item_listid AS item_id,
       l.item_fullname AS item_name, l.line_tag,
       CASE
         WHEN t.txn_kind IN ('invoice','salesreceipt') THEN -CAST(l.quantity AS REAL)
         WHEN t.txn_kind IN ('creditmemo','bill','itemreceipt','check','cccharge')
           THEN CAST(l.quantity AS REAL)
         WHEN t.txn_kind IN ('vendorcredit','cccredit') THEN -CAST(l.quantity AS REAL)
         WHEN t.txn_kind = 'invadjust' THEN CAST(l.quantity AS REAL)
         WHEN t.txn_kind = 'buildassembly' THEN
           CASE WHEN l.line_tag = 'ComponentItemLineRet' THEN -CAST(l.quantity AS REAL)
                ELSE CAST(l.quantity AS REAL) END
       END AS qty_delta,
       CASE
         WHEN t.txn_kind IN ('bill','itemreceipt','check','cccharge') THEN l.amount_c
         WHEN t.txn_kind IN ('vendorcredit','cccredit') THEN -l.amount_c
         WHEN t.txn_kind = 'invadjust' THEN l.amount_c
       END AS value_delta_c,
       t.src_key
FROM fact_txn_line l
JOIN fact_txn t USING (txn_id)
JOIN v_item i ON i.id = l.item_listid
WHERE t.txn_kind IN ('invoice','salesreceipt','creditmemo','bill','itemreceipt','check',
                     'cccharge','cccredit','vendorcredit','invadjust','buildassembly')
  AND l.quantity IS NOT NULL AND l.quantity <> ''
  AND COALESCE(t.is_pending,'false') <> 'true'
  AND i.item_type IN ('Inventory','InventoryAssembly');
