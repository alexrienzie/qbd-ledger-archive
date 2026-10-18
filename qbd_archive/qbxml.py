"""qbXML request builders and the object catalog.

Element order matters: QuickBooks validates every request against the qbXML
schema, and an element in the wrong place (or one a query does not accept)
fails the whole request with a parse error before any data is read. The
builders below emit elements in schema order.
"""

# Lists: tag -> QueryRq name. Each response may hold several Ret tags (items
# come back as ItemInventoryRet, ItemServiceRet, ...), so the loader takes
# every child that carries a ListID.
LISTS = {
    "account": "AccountQueryRq",
    "class": "ClassQueryRq",
    "terms": "TermsQueryRq",
    "customer": "CustomerQueryRq",
    "vendor": "VendorQueryRq",
    "item": "ItemQueryRq",
    "othername": "OtherNameQueryRq",
    "employee": "EmployeeQueryRq",
    "salestaxcode": "SalesTaxCodeQueryRq",
    "paymentmethod": "PaymentMethodQueryRq",
}

# Source documents: tag -> (QueryRq, Ret, posting?).
# posting=False marks non-posting documents (orders, estimates) that never
# reach the general ledger.
TXN_TYPES = {
    "invoice":       ("InvoiceQueryRq", "InvoiceRet", True),
    "salesreceipt":  ("SalesReceiptQueryRq", "SalesReceiptRet", True),
    "recvpayment":   ("ReceivePaymentQueryRq", "ReceivePaymentRet", True),
    "creditmemo":    ("CreditMemoQueryRq", "CreditMemoRet", True),
    "arrefundcc":    ("ARRefundCreditCardQueryRq", "ARRefundCreditCardRet", True),
    "deposit":       ("DepositQueryRq", "DepositRet", True),
    "bill":          ("BillQueryRq", "BillRet", True),
    "billpayck":     ("BillPaymentCheckQueryRq", "BillPaymentCheckRet", True),
    "billpaycc":     ("BillPaymentCreditCardQueryRq", "BillPaymentCreditCardRet", True),
    "check":         ("CheckQueryRq", "CheckRet", True),
    "cccharge":      ("CreditCardChargeQueryRq", "CreditCardChargeRet", True),
    "cccredit":      ("CreditCardCreditQueryRq", "CreditCardCreditRet", True),
    "vendorcredit":  ("VendorCreditQueryRq", "VendorCreditRet", True),
    "itemreceipt":   ("ItemReceiptQueryRq", "ItemReceiptRet", True),
    "journal":       ("JournalEntryQueryRq", "JournalEntryRet", True),
    "transfer":      ("TransferQueryRq", "TransferRet", True),
    "invadjust":     ("InventoryAdjustmentQueryRq", "InventoryAdjustmentRet", True),
    "buildassembly": ("BuildAssemblyQueryRq", "BuildAssemblyRet", True),
    "salestaxpayck": ("SalesTaxPaymentCheckQueryRq", "SalesTaxPaymentCheckRet", True),
    "charge":        ("ChargeQueryRq", "ChargeRet", True),              # statement charges
    "transferinventory": ("TransferInventoryQueryRq", "TransferInventoryRet", True),  # Enterprise
    "salesorder":    ("SalesOrderQueryRq", "SalesOrderRet", False),
    "purchaseorder": ("PurchaseOrderQueryRq", "PurchaseOrderRet", False),
    "estimate":      ("EstimateQueryRq", "EstimateRet", False),
}

# The line-item flag is not uniform across queries. BuildAssembly takes
# IncludeComponentLineItems; Transfer has no line items at all. Sending the
# wrong element is a schema parse error, so this table is explicit.
LINE_ITEM_FLAG = {tag: "IncludeLineItems" for tag in TXN_TYPES}
LINE_ITEM_FLAG["buildassembly"] = "IncludeComponentLineItems"
LINE_ITEM_FLAG["transfer"] = None
LINE_ITEM_FLAG["charge"] = None  # a statement charge is a single line on its header

# Queries we ask for linked transactions (which credit memo or payment
# settled which invoice, which receipt filled which PO). Payments carry their
# applications as AppliedToTxnRet without this flag. If QuickBooks rejects the
# flag for a type, the extractor drops it for that type and records the fact
# in the manifest, so the gap is visible in validation.
LINKED_TXNS = {"invoice", "creditmemo", "salesreceipt", "bill", "check",
               "cccharge", "vendorcredit", "itemreceipt", "journal",
               "salesorder", "purchaseorder", "estimate"}

# Types whose line items cross-reference many other transactions; they get a
# short timeout so an oversized window bisects quickly.
CROSSREF_TYPES = {"recvpayment", "billpayck", "billpaycc", "deposit"}

# Deletion logs. QuickBooks keeps roughly the last 90 days; on an archive of
# frozen books "none" is the expected answer, and recording it is the point.
TXNDEL_TYPES = ["Bill", "BillPaymentCheck", "BillPaymentCreditCard", "Check",
                "CreditCardCharge", "CreditCardCredit", "CreditMemo", "Deposit",
                "InventoryAdjustment", "Invoice", "ItemReceipt", "JournalEntry",
                "PurchaseOrder", "ReceivePayment", "SalesOrder", "SalesReceipt",
                "VendorCredit"]
LISTDEL_TYPES = ["Account", "Class", "Customer", "Employee", "OtherName",
                 "Vendor", "ItemInventory", "ItemNonInventory", "ItemService"]

SINGLES = {
    "company": '<CompanyQueryRq requestID="1"/>',
    "preferences": '<PreferencesQueryRq requestID="1"/>',
    "txndeleted": ('<TxnDeletedQueryRq requestID="1">'
                   + "".join(f"<TxnDelType>{t}</TxnDelType>" for t in TXNDEL_TYPES)
                   + "</TxnDeletedQueryRq>"),
    "listdeleted": ('<ListDeletedQueryRq requestID="1">'
                    + "".join(f"<ListDelType>{t}</ListDelType>" for t in LISTDEL_TYPES)
                    + "</ListDeletedQueryRq>"),
}

# Columns requested on the General Ledger detail report. TxnNumber is the
# join key between the ledger and the source documents (the report carries
# no TxnID). Account is requested so a row can be placed even when the
# report omits its section header.
GL_COLUMNS = ["TxnType", "Date", "RefNumber", "TxnNumber", "Name", "Memo",
              "Account", "Debit", "Credit", "RunningBalance"]

SUMMARY_REPORT = {"tb": "TrialBalance", "pnl": "ProfitAndLossStandard",
                  "bs": "BalanceSheetStandard"}
REPORT_TITLE = {"gl": "general ledger", "tb": "trial balance",
                "pnl": "profit", "bs": "balance sheet"}
BASIS_VALUE = {"accrual": "Accrual", "cash": "Cash"}


def envelope(body, version, on_error="stopOnError"):
    return (f'<?xml version="1.0"?><?qbxml version="{version}"?>'
            f'<QBXML><QBXMLMsgsRq onError="{on_error}">{body}'
            "</QBXMLMsgsRq></QBXML>")


def _period(frm, to):
    return (f"<ReportPeriod><FromReportDate>{frm}</FromReportDate>"
            f"<ToReportDate>{to}</ToReportDate></ReportPeriod>")


def gl_report(frm, to, basis):
    """General Ledger detail. The basis is always explicit: left out, the
    report follows the company preference (often cash), and a cash-basis
    ledger will not tie to an accrual trial balance. On the detail report
    ReportBasis goes after the IncludeColumn list; placed before it, the
    request fails the schema check."""
    cols = "".join(f"<IncludeColumn>{c}</IncludeColumn>" for c in GL_COLUMNS)
    return ('<GeneralDetailReportQueryRq requestID="1">'
            "<GeneralDetailReportType>GeneralLedger</GeneralDetailReportType>"
            f"{_period(frm, to)}{cols}<ReportBasis>{BASIS_VALUE[basis]}</ReportBasis>"
            "</GeneralDetailReportQueryRq>")


def summary_report(kind, frm, to, basis):
    return ('<GeneralSummaryReportQueryRq requestID="1">'
            f"<GeneralSummaryReportType>{SUMMARY_REPORT[kind]}</GeneralSummaryReportType>"
            f"{_period(frm, to)}<ReportBasis>{BASIS_VALUE[basis]}</ReportBasis>"
            "</GeneralSummaryReportQueryRq>")


def list_query(tag):
    rq = LISTS[tag]
    return f'<{rq} requestID="1"><ActiveStatus>All</ActiveStatus></{rq}>'


def txn_query(tag, frm, to, line_items=True, linked_txns=True):
    rq = TXN_TYPES[tag][0]
    inner = (f"<TxnDateRangeFilter><FromTxnDate>{frm}</FromTxnDate>"
             f"<ToTxnDate>{to}</ToTxnDate></TxnDateRangeFilter>")
    flag = LINE_ITEM_FLAG.get(tag)
    if line_items and flag:
        inner += f"<{flag}>true</{flag}>"
    if linked_txns and tag in LINKED_TXNS:
        inner += "<IncludeLinkedTxns>true</IncludeLinkedTxns>"
    return f'<{rq} requestID="1">{inner}</{rq}>'


def count_probe(tags_lists, tags_txns):
    """One request holding a metaData-only count query per object type.
    QuickBooks returns retCount without the records: the completeness
    benchmark the loaded archive is checked against."""
    parts = []
    n = 0
    for tag in tags_lists:
        n += 1
        rq = LISTS[tag]
        parts.append(f'<{rq} requestID="{n}" metaData="MetaDataOnly">'
                     f"<ActiveStatus>All</ActiveStatus></{rq}>")
    for tag in tags_txns:
        n += 1
        rq = TXN_TYPES[tag][0]
        parts.append(f'<{rq} requestID="{n}" metaData="MetaDataOnly"/>')
    return "".join(parts)
