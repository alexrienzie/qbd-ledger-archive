"""End-to-end extraction against a fake QuickBooks: extract, load, validate."""
import json
import re
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qbd_archive import config, extract, load, validate  # noqa: E402
from qbd_archive.transport import TransportError  # noqa: E402
from tests import fixtures  # noqa: E402

LISTS = {"AccountQueryRq": fixtures.account_list, "VendorQueryRq": fixtures.vendor_list,
         "CustomerQueryRq": fixtures.customer_list}
DOCS = {"InvoiceQueryRq": [fixtures.INVOICE, fixtures.INVOICE],  # an SDK re-emission
        "ReceivePaymentQueryRq": [fixtures.PAYMENT],
        "CheckQueryRq": [c + "</CheckRet>" for c in fixtures.CHECKS.split("</CheckRet>") if c]}


class FakeQuickBooks:
    """Answers qbXML like QuickBooks would for the sample company. Invoice
    windows longer than `max_days` time out, so the extractor has to split."""

    def __init__(self, max_days=10):
        self.max_days = max_days
        self.requests = []

    def send(self, request_xml, timeout):
        self.requests.append(request_xml)
        msgs = ET.fromstring(request_xml).find("QBXMLMsgsRq")
        rq = msgs[0]
        rid = rq.get("requestID")
        name = rq.tag
        rs = name.replace("Rq", "Rs")

        def wrap(xml):
            return re.sub(r'requestID="\d+"', f'requestID="{rid}"', xml, count=1)

        if rq.get("metaData") == "MetaDataOnly":
            n = {"AccountQueryRq": len(fixtures.ACCOUNTS), "VendorQueryRq": 1,
                 "CustomerQueryRq": 2}.get(name, len(DOCS.get(name, [])))
            return (f'<QBXML><QBXMLMsgsRs><{rs} requestID="{rid}" statusCode="{0 if n else 1}" '
                    f'retCount="{n}"/></QBXMLMsgsRs></QBXML>')
        if name == "CompanyQueryRq":
            return wrap(fixtures.rs("CompanyQueryRs", "<CompanyRet><CompanyName>Sample</CompanyName>"
                                    "<FirstMonthFiscalYear>January</FirstMonthFiscalYear>"
                                    "</CompanyRet>"))
        if name in LISTS:
            return wrap(LISTS[name]())
        if name == "GeneralDetailReportQueryRq":
            frm = rq.findtext("ReportPeriod/FromReportDate")
            return wrap(fixtures.gl_2020_01() if frm == "2020-01-01" else fixtures.empty_gl())
        if name == "GeneralSummaryReportQueryRq":
            kind = rq.findtext("GeneralSummaryReportType")
            frm = rq.findtext("ReportPeriod/FromReportDate")
            fy = frm == "2020-01-01"
            if kind == "TrialBalance":
                return wrap(fixtures.tb(fixtures.TB_2020 if fy else []))
            if kind.startswith("ProfitAndLoss"):
                return wrap(fixtures.pnl("128.00" if fy else "0.00"))
            return wrap(fixtures.bs())
        if name.endswith("QueryRq") and rq.find("TxnDateRangeFilter") is not None:
            frm = rq.findtext("TxnDateRangeFilter/FromTxnDate")
            to = rq.findtext("TxnDateRangeFilter/ToTxnDate")
            if name == "InvoiceQueryRq" and frm >= "2020-01-01" and to <= "2020-12-31":
                import datetime as dt
                days = (dt.date.fromisoformat(to) - dt.date.fromisoformat(frm)).days
                if days > self.max_days:
                    raise TransportError(f"timeout after {timeout}s")
            docs = [d for d in DOCS.get(name, [])
                    if frm <= re.search(r"<TxnDate>([^<]+)", d).group(1) <= to]
            return (f'<QBXML><QBXMLMsgsRs><{rs} requestID="{rid}" '
                    f'statusCode="{0 if docs else 1}">{"".join(docs)}</{rs}></QBXMLMsgsRs></QBXML>')
        # deletion logs and anything else: a valid empty answer
        return f'<QBXML><QBXMLMsgsRs><{rs} requestID="{rid}" statusCode="1"/></QBXMLMsgsRs></QBXML>'


class Extraction(unittest.TestCase):
    def test_extract_load_validate(self):
        with tempfile.TemporaryDirectory() as td:
            cfgp = Path(td) / "archive.toml"
            cfgp.write_text('[archive]\ndir = "archive"\ncompany_label = "t"\n'
                            '[books]\nstart = "2020-01-01"\nend = "2020-01-31"\n'
                            '[extract]\ntxn_types = ["invoice", "recvpayment", "check"]\n')
            cfg = config.load(cfgp)
            cfg.archive_dir.mkdir()
            qb = FakeQuickBooks(max_days=10)
            extract.time.sleep = lambda s: None  # no real backoff in tests
            rc = extract.main(cfg, qb, ["probe", "singles", "lists", "reports", "txns"])
            self.assertEqual(rc, 0)
            man = json.loads(cfg.manifest_path.read_text())
            inv = [k for k in man["chunks"] if k.startswith("txn_invoice_2020")]
            self.assertGreater(len(inv), 1, "a 31-day invoice window should have been split")
            self.assertTrue(all(man["types"][t]["status"] == "complete"
                                for t in ("invoice", "recvpayment", "check")))
            self.assertFalse((cfg.archive_dir / "extract.lock").exists())
            load.Loader(cfg).build()
            self.assertEqual(validate.main(cfg), 0)
            gates = {g["name"]: g["status"] for g in
                     json.loads((cfg.validation_dir / "status.json").read_text())["gates"]}
            for g in ("plan_coverage", "tieout", "pnl_net_income", "completeness",
                      "fiscal_year_setting", "line_detail"):
                self.assertEqual(gates[g], "pass", g)

    def test_collision_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            cfgp = Path(td) / "archive.toml"
            cfgp.write_text('[archive]\ndir = "archive"\n[books]\nstart = "2020-01-01"\n'
                            'end = "2020-01-31"\n')
            cfg = config.load(cfgp)
            cfg.archive_dir.mkdir()

            class Wrong(FakeQuickBooks):
                def send(self, request_xml, timeout):
                    out = super().send(request_xml, timeout)
                    return re.sub(r'requestID="\d+"', 'requestID="123"', out, count=1)

            extract.time.sleep = lambda s: None
            with extract.Extractor(cfg, Wrong()) as ex:
                root, err = ex.pull("list_account", "<AccountQueryRq requestID=\"1\">"
                                    "<ActiveStatus>All</ActiveStatus></AccountQueryRq>",
                                    ex._plain_checker("AccountQueryRs"), 10, attempts=1)
            self.assertIsNone(root)
            self.assertIn("collision", err)


if __name__ == "__main__":
    unittest.main()
