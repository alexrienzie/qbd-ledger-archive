"""A tiny synthetic company, written as the qbXML QuickBooks would return.

Chart of accounts:
  Checking (Bank)
  Accounts Receivable (AccountsReceivable)
  Retained Earnings (Equity)
  Sales (Income) with children Sales:Retail and Sales:Wholesale
  Wholesale:Freight and Retail:Freight would be ambiguous, so the chart also
  has Expenses:Freight and Shipping:Freight (two accounts named "Freight")
  Expenses (Expense) parent of Expenses:Freight; Shipping (Expense) parent of
  Shipping:Freight

One fiscal year (2020) of activity, with:
  * a header-less stretch of rows (the missing-section-header defect)
  * a duplicate invoice emission (byte-identical)
  * a direct posting to a parent with children ("Sales - Other")
"""
import json
from pathlib import Path

ACCOUNTS = [
    ("A1", "Checking", "Checking", "Bank", None),
    ("A2", "Accounts Receivable", "Accounts Receivable", "AccountsReceivable", None),
    ("A3", "Retained Earnings", "Retained Earnings", "Equity", None),
    ("A4", "Sales", "Sales", "Income", None),
    ("A5", "Retail", "Sales:Retail", "Income", "A4"),
    ("A6", "Wholesale", "Sales:Wholesale", "Income", "A4"),
    ("A7", "Expenses", "Expenses", "Expense", None),
    ("A8", "Freight", "Expenses:Freight", "Expense", "A7"),
    ("A9", "Shipping", "Shipping", "Expense", None),
    ("A10", "Freight", "Shipping:Freight", "Expense", "A9"),
]


def rs(tag, inner, status="0"):
    return (f'<?xml version="1.0"?><QBXML><QBXMLMsgsRs><{tag} requestID="1" '
            f'statusCode="{status}" statusMessage="Status OK">{inner}</{tag}>'
            "</QBXMLMsgsRs></QBXML>")


def account_list():
    out = []
    for lid, name, full, typ, parent in ACCOUNTS:
        p = f"<ParentRef><ListID>{parent}</ListID></ParentRef>" if parent else ""
        out.append(f"<AccountRet><ListID>{lid}</ListID><Name>{name}</Name>"
                   f"<FullName>{full}</FullName><IsActive>true</IsActive>{p}"
                   f"<Sublevel>{full.count(':')}</Sublevel><AccountType>{typ}</AccountType>"
                   "</AccountRet>")
    return rs("AccountQueryRs", "".join(out))


def vendor_list():
    # VendorTypeRef carries its own FullName: the trap for naive parsers
    return rs("VendorQueryRs",
              "<VendorRet><ListID>V1</ListID><Name>Freight Co</Name><IsActive>true</IsActive>"
              "<VendorTypeRef><ListID>VT1</ListID><FullName>Carriers</FullName></VendorTypeRef>"
              "</VendorRet>")


def customer_list():
    return rs("CustomerQueryRs",
              "<CustomerRet><ListID>C1</ListID><Name>Customer A</Name><FullName>Customer A</FullName>"
              "<IsActive>true</IsActive></CustomerRet>"
              "<CustomerRet><ListID>C2</ListID><Name>Project A</Name><FullName>Customer A:Project A</FullName>"
              "<IsActive>true</IsActive><ParentRef><ListID>C1</ListID></ParentRef></CustomerRet>")


GL_COLS = ["Type", "Date", "Num", "Trans #", "Name", "Memo", "Account", "Debit",
           "Credit", "Balance"]


def _row(tag, vals):
    cells = "".join(f'<ColData colID="{i}" value="{v}"/>' for i, v in vals.items() if v is not None)
    return f"<{tag}>{cells}</{tag}>"


def hdr(label, balance=None):
    return _row("DataRow", {1: label, 11: balance})


def total(label):
    return _row("SubtotalRow", {1: f"Total {label}"})


def post(typ, date, num, txn, acct_short, debit=None, credit=None, name="Customer A"):
    return _row("DataRow", {2: typ, 3: date, 4: num, 5: txn, 6: name, 8: acct_short,
                            9: debit, 10: credit})


def gl_report(rows, title="General Ledger"):
    cols = "".join(f'<ColDesc colID="{i}"><ColTitle titleRow="1" value="{t}"/></ColDesc>'
                   for i, t in enumerate(["", *GL_COLS], 1) if t)
    return rs("GeneralDetailReportQueryRs",
              f"<ReportRet><ReportTitle>{title}</ReportTitle>{cols}"
              f"<ReportData>{''.join(rows)}</ReportData></ReportRet>")


def gl_2020_01():
    """January: invoice 100 retail + 50 wholesale; receive 150; freight 20 (both
    Freight accounts appear in a header-less region); a 5.00 direct posting to
    the Sales parent."""
    return gl_report([
        hdr("Checking", "1000.00"),
        post("Payment", "2020-01-10", "P1", "3", "Checking", debit="150.00"),
        post("Check", "2020-01-12", "101", "4", "Checking", credit="20.00", name="Freight Co"),
        post("Check", "2020-01-13", "102", "5", "Checking", credit="7.00", name="Freight Co"),
        total("Checking"),
        hdr("Accounts Receivable", "0.00"),
        post("Invoice", "2020-01-05", "1001", "1", "Accounts Receivable", debit="155.00"),
        post("Payment", "2020-01-10", "P1", "3", "Accounts Receivable", credit="150.00"),
        total("Accounts Receivable"),
        hdr("Retained Earnings", "-1000.00"),
        _row("DataRow", {1: "Total Retained Earnings"}),  # closed by a DataRow
        hdr("Sales"),                     # aggregate parent: no beginning balance
        hdr("Retail", "0.00"),
        post("Invoice", "2020-01-05", "1001", "1", "Retail", credit="100.00"),
        total("Retail"),
        # child header for Wholesale is missing; the row arrives under Sales
        post("Invoice", "2020-01-05", "1001", "1", "Wholesale", credit="50.00"),
        hdr("Sales - Other", "0.00"),
        post("Invoice", "2020-01-05", "1001", "1", "Sales", credit="5.00"),
        total("Sales - Other"),
        total("Sales"),
        # header-less region: two accounts named "Freight"; the subtotals
        # that follow name the parent of each
        post("Check", "2020-01-12", "101", "4", "Freight", debit="20.00", name="Freight Co"),
        total("Freight"),
        total("Expenses"),
        post("Check", "2020-01-13", "102", "5", "Freight", debit="7.00", name="Freight Co"),
        total("Freight"),
        total("Shipping"),
    ])


def tb(period_rows):
    cols = ('<ColDesc colID="1"><ColTitle titleRow="1" value=""/></ColDesc>'
            '<ColDesc colID="2"><ColTitle titleRow="1" value="Debit"/></ColDesc>'
            '<ColDesc colID="3"><ColTitle titleRow="1" value="Credit"/></ColDesc>')
    rows = "".join(_row("DataRow", {1: a, 2: d, 3: c}) for a, d, c in period_rows)
    return rs("GeneralSummaryReportQueryRs",
              f"<ReportRet><ReportTitle>Trial Balance</ReportTitle>{cols}"
              f"<ReportData>{rows}</ReportData></ReportRet>")


def pnl(net_income):
    rows = (_row("TotalRow", {1: "Net Income", 2: net_income}))
    return rs("GeneralSummaryReportQueryRs",
              f"<ReportRet><ReportTitle>Profit &amp; Loss</ReportTitle>"
              f"<ReportData>{rows}</ReportData></ReportRet>")


def bs():
    return rs("GeneralSummaryReportQueryRs",
              "<ReportRet><ReportTitle>Balance Sheet</ReportTitle><ReportData/></ReportRet>")


def empty_gl():
    return gl_report([])


INVOICE = ("<InvoiceRet><TxnID>T1</TxnID><TxnNumber>1</TxnNumber><TxnDate>2020-01-05</TxnDate>"
           "<RefNumber>1001</RefNumber><CustomerRef><ListID>C1</ListID><FullName>Customer A</FullName>"
           "</CustomerRef><ARAccountRef><ListID>A2</ListID><FullName>Accounts Receivable</FullName>"
           "</ARAccountRef><Subtotal>155.00</Subtotal>"
           "<InvoiceLineRet><TxnLineID>L1</TxnLineID><Desc>Retail goods</Desc>"
           "<Quantity>2</Quantity><Rate>50.00</Rate><Amount>100.00</Amount></InvoiceLineRet>"
           "<InvoiceLineRet><TxnLineID>L2</TxnLineID><Desc>Wholesale goods</Desc>"
           "<Amount>50.00</Amount></InvoiceLineRet>"
           "<InvoiceLineRet><TxnLineID>L3</TxnLineID><Desc>Misc</Desc><Amount>5.00</Amount>"
           "</InvoiceLineRet></InvoiceRet>")
PAYMENT = ("<ReceivePaymentRet><TxnID>T3</TxnID><TxnNumber>3</TxnNumber>"
           "<TxnDate>2020-01-10</TxnDate><CustomerRef><ListID>C1</ListID><FullName>Customer A"
           "</FullName></CustomerRef><TotalAmount>150.00</TotalAmount>"
           "<AppliedToTxnRet><TxnID>T1</TxnID><TxnType>Invoice</TxnType><Amount>150.00</Amount>"
           "</AppliedToTxnRet></ReceivePaymentRet>")
CHECKS = ("<CheckRet><TxnID>T4</TxnID><TxnNumber>4</TxnNumber><TxnDate>2020-01-12</TxnDate>"
          "<AccountRef><ListID>A1</ListID><FullName>Checking</FullName></AccountRef>"
          "<PayeeEntityRef><ListID>V1</ListID><FullName>Freight Co</FullName></PayeeEntityRef>"
          "<Amount>20.00</Amount><ExpenseLineRet><TxnLineID>E1</TxnLineID><AccountRef>"
          "<ListID>A8</ListID><FullName>Expenses:Freight</FullName></AccountRef>"
          "<Amount>20.00</Amount></ExpenseLineRet></CheckRet>"
          "<CheckRet><TxnID>T5</TxnID><TxnNumber>5</TxnNumber><TxnDate>2020-01-13</TxnDate>"
          "<AccountRef><ListID>A1</ListID><FullName>Checking</FullName></AccountRef>"
          "<PayeeEntityRef><ListID>V1</ListID><FullName>Freight Co</FullName></PayeeEntityRef>"
          "<Amount>7.00</Amount><ExpenseLineRet><TxnLineID>E2</TxnLineID><AccountRef>"
          "<ListID>A10</ListID><FullName>Shipping:Freight</FullName></AccountRef>"
          "<Amount>7.00</Amount></ExpenseLineRet></CheckRet>")

TB_2020 = [("Checking", "1123.00", None), ("Accounts Receivable", "5.00", None),
           ("Retained Earnings", None, "1000.00"), ("Sales", None, "5.00"),
           ("Sales:Retail", None, "100.00"), ("Sales:Wholesale", None, "50.00"),
           ("Expenses:Freight", "20.00", None), ("Shipping:Freight", "7.00", None)]


def write_archive(root, tb_rows=TB_2020, net_income="128.00", probe_invoice=2,
                  collapsed_year=False, skip_chunk=None, duplicate_across_windows=False):
    """A complete archive dir for books 2020-01-01..2020-01-31, with every
    report chunk the period plan calls for. Returns the config path.

    skip_chunk: leave out the planned report chunk whose key starts with this
    duplicate_across_windows: the duplicate invoice comes back in a second
    window instead of inside the same response"""
    from qbd_archive import config as config_mod

    root = Path(root)
    raw = root / "archive" / "raw"
    raw.mkdir(parents=True)
    cfg_path = root / "archive.toml"
    cfg_path.write_text('[archive]\ndir = "archive"\ncompany_label = "test"\n'
                        '[books]\nstart = "2020-01-01"\nend = "2020-01-31"\n'
                        '[extract]\ntxn_types = ["invoice", "recvpayment", "check"]\n')
    cfg = config_mod.load(cfg_path)
    chunks, types = {}, {}

    def put(key, xml, **meta):
        (raw / f"{key}.xml").write_text(xml, encoding="utf-8")
        chunks[key] = {"status": "ok", "file": f"{key}.xml", **meta}

    put("list_account", account_list(), kind="list", tag="account")
    put("list_vendor", vendor_list(), kind="list", tag="vendor")
    put("list_customer", customer_list(), kind="list", tag="customer")
    pre_gl = empty_gl() if not collapsed_year else gl_report([
        hdr("Retained Earnings", "0.00"),
        post("Closing Entry ", "2019-12-31", None, "0", "Retained Earnings", credit="35.00",
             name=None),
        total("Retained Earnings")])
    for key, kind, basis, period, frm, to in cfg.report_plan():
        if skip_chunk and key.startswith(skip_chunk):
            continue
        meta = {"kind": kind, "basis": basis, "period": period, "from": frm, "to": to}
        if kind == "gl":
            xml = {"pre": pre_gl, "FY2020": gl_2020_01()}.get(period, empty_gl())
        elif kind == "tb":
            xml = tb(tb_rows) if period == "FY2020" else tb([])
        elif kind == "pnl":
            xml = pnl(net_income) if period == "FY2020" else pnl("0.00")
        else:
            xml = bs()
        put(key, xml, **meta)
    if duplicate_across_windows:
        put("txn_invoice_20200101_20200115", rs("InvoiceQueryRs", INVOICE),
            kind="txn", tag="invoice", **{"from": "2020-01-01", "to": "2020-01-15"})
        put("txn_invoice_20200116_20200131", rs("InvoiceQueryRs", INVOICE),
            kind="txn", tag="invoice", **{"from": "2020-01-16", "to": "2020-01-31"})
    else:
        # the invoice is emitted twice in one response (an SDK re-emission)
        put("txn_invoice_20200101_20200131", rs("InvoiceQueryRs", INVOICE + INVOICE),
            kind="txn", tag="invoice", **{"from": "2020-01-01", "to": "2020-01-31"})
    put("txn_recvpayment_20200101_20200131", rs("ReceivePaymentQueryRs", PAYMENT),
        kind="txn", tag="recvpayment", **{"from": "2020-01-01", "to": "2020-01-31"})
    put("txn_check_20200101_20200131", rs("CheckQueryRs", CHECKS),
        kind="txn", tag="check", **{"from": "2020-01-01", "to": "2020-01-31"})
    for t in ("invoice", "recvpayment", "check"):
        types[t] = {"status": "complete", "windows": [], "gaps": [], "linked_txns": True}
    probe = (f'<AccountQueryRs requestID="1" statusCode="0" retCount="{len(ACCOUNTS)}"/>'
             '<VendorQueryRs requestID="2" statusCode="0" retCount="1"/>'
             '<CustomerQueryRs requestID="3" statusCode="0" retCount="2"/>'
             f'<InvoiceQueryRs requestID="4" statusCode="0" retCount="{probe_invoice}"/>'
             '<ReceivePaymentQueryRs requestID="5" statusCode="0" retCount="1"/>'
             '<CheckQueryRs requestID="6" statusCode="0" retCount="2"/>')
    put("probe_counts", f"<QBXML><QBXMLMsgsRs>{probe}</QBXMLMsgsRs></QBXML>", kind="probe")
    (root / "archive" / "manifest.json").write_text(
        json.dumps({"format": 1, "chunks": chunks, "types": types}))
    return cfg_path
