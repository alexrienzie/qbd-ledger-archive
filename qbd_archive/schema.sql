-- qbd-ledger-archive schema.
-- Conventions: integer cents in *_c columns; ISO dates; QuickBooks ListID /
-- TxnID / TxnLineID kept verbatim; every row carries src_key (the manifest
-- key of the raw response it came from) so any number can be traced back
-- to the exact qbXML QuickBooks returned.

CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE source_file(
  src_key TEXT PRIMARY KEY, kind TEXT, basis TEXT, period TEXT,
  period_from TEXT, period_to TEXT, file TEXT, sha256 TEXT, bytes INTEGER,
  pulled_at TEXT);

-- ---------- lists ----------
CREATE TABLE dim_list(
  list_type TEXT, list_id TEXT, name TEXT, fullname TEXT, is_active TEXT,
  parent_listid TEXT, record_tag TEXT, time_created TEXT, time_modified TEXT,
  edit_sequence TEXT, xml TEXT, src_key TEXT,
  UNIQUE(list_type, list_id));

CREATE TABLE dim_account(
  list_id TEXT PRIMARY KEY, name TEXT, fullname TEXT UNIQUE, account_type TEXT,
  special_account_type TEXT, account_number TEXT, parent_listid TEXT,
  is_active TEXT, sublevel INTEGER);

CREATE TABLE map_item_account(
  item_listid TEXT PRIMARY KEY, item_fullname TEXT, item_record_tag TEXT,
  income_listid TEXT, income_fullname TEXT, cogs_listid TEXT, cogs_fullname TEXT,
  asset_listid TEXT, asset_fullname TEXT, expense_listid TEXT, expense_fullname TEXT,
  generic_listid TEXT, generic_fullname TEXT);

-- ---------- ledger and reports ----------
CREATE TABLE gl_line(
  id INTEGER PRIMARY KEY, basis TEXT, txn_type TEXT, txn_date TEXT,
  ref_number TEXT, txn_number TEXT, name TEXT, memo TEXT,
  account_fullname TEXT, account_src TEXT, account_col TEXT,
  debit_c INTEGER, credit_c INTEGER, running_balance_c INTEGER,
  src_key TEXT, src_row INTEGER);

CREATE TABLE gl_beginning_balance(
  basis TEXT, account_fullname TEXT, as_of TEXT, balance_c INTEGER,
  src_key TEXT, src_row INTEGER);

CREATE TABLE rpt_tb(
  basis TEXT, period TEXT, period_from TEXT, period_to TEXT,
  account_fullname TEXT, debit_c INTEGER, credit_c INTEGER, src_key TEXT);

CREATE TABLE rpt_summary(
  kind TEXT, basis TEXT, period TEXT, seq INTEGER, row_type TEXT, label TEXT,
  amount_c INTEGER, src_key TEXT);

-- ---------- source documents ----------
CREATE TABLE fact_txn(
  txn_id TEXT PRIMARY KEY, txn_kind TEXT, ret_tag TEXT, txn_number TEXT,
  txn_date TEXT, ref_number TEXT, entity_listid TEXT, entity_fullname TEXT,
  account_listid TEXT, account_fullname TEXT, amount_c INTEGER, memo TEXT,
  is_pending TEXT, time_created TEXT, time_modified TEXT, edit_sequence TEXT,
  -- sales tax and nexus fields (sales documents; NULL elsewhere)
  subtotal_c INTEGER, sales_tax_c INTEGER, sales_tax_pct TEXT, sales_tax_item TEXT,
  customer_tax_code TEXT, ship_state TEXT, ship_postal TEXT, ship_country TEXT,
  bill_state TEXT,
  src_key TEXT);

CREATE TABLE fact_txn_line(
  txn_id TEXT, txn_line_id TEXT, line_id_synthetic INTEGER, seq INTEGER,
  line_tag TEXT, item_listid TEXT, item_fullname TEXT, descr TEXT,
  quantity TEXT, rate TEXT, amount_c INTEGER, account_listid TEXT,
  account_fullname TEXT, class_fullname TEXT, entity_fullname TEXT,
  UNIQUE(txn_id, txn_line_id));

CREATE TABLE fact_txn_link(
  txn_id TEXT, linked_txn_id TEXT, linked_txn_type TEXT, link_kind TEXT,
  amount_c INTEGER);

-- ---------- load and extraction evidence ----------
-- Every duplicate the SDK emitted, with whether it was byte-identical to the
-- copy kept, and whether both copies came back in the same response (an SDK
-- re-emission). identical=0 is a conflict and fails validation.
CREATE TABLE load_duplicate(
  scope TEXT, object_id TEXT, src_key TEXT, kept_src_key TEXT, identical INTEGER,
  same_response INTEGER);

CREATE TABLE probe_count(
  object_type TEXT, query TEXT, status_code TEXT, ret_count INTEGER, message TEXT);

CREATE TABLE extract_type(
  txn_kind TEXT PRIMARY KEY, status TEXT, windows INTEGER, gaps TEXT,
  linked_txns INTEGER);

CREATE INDEX ix_gl_acct_date ON gl_line(basis, account_fullname, txn_date);
CREATE INDEX ix_gl_date ON gl_line(basis, txn_date);
CREATE INDEX ix_gl_txnnum ON gl_line(txn_number);
CREATE INDEX ix_gl_src ON gl_line(src_key, src_row);
CREATE INDEX ix_txn_num ON fact_txn(txn_number);
CREATE INDEX ix_txn_kind_date ON fact_txn(txn_kind, txn_date);
CREATE INDEX ix_txn_entity ON fact_txn(entity_fullname);
CREATE INDEX ix_line_item ON fact_txn_line(item_listid);
CREATE INDEX ix_link_txn ON fact_txn_link(txn_id);
CREATE INDEX ix_link_target ON fact_txn_link(linked_txn_id);
