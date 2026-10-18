import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qbd_archive import config, gl, load, money, qbxml, validate  # noqa: E402
from tests import fixtures  # noqa: E402


class Money(unittest.TestCase):
    def test_cents(self):
        self.assertEqual(money.cents("1,234.50"), 123450)
        self.assertEqual(money.cents("-0.07"), -7)
        self.assertIsNone(money.cents(""))
        with self.assertRaises(ValueError):
            money.cents("1.005")
        with self.assertRaises(ValueError):
            money.cents("abc")

    def test_dates(self):
        self.assertEqual(money.norm_date("3/7/19"), "2019-03-07")
        self.assertEqual(money.norm_date("2019-03-07"), "2019-03-07")
        self.assertIsNone(money.norm_date("Total"))


class Requests(unittest.TestCase):
    def test_gl_basis_after_columns(self):
        x = qbxml.gl_report("2020-01-01", "2020-01-31", "accrual")
        self.assertLess(x.rindex("<IncludeColumn>"), x.index("<ReportBasis>Accrual"))

    def test_buildassembly_line_flag(self):
        x = qbxml.txn_query("buildassembly", "2020-01-01", "2020-01-31")
        self.assertIn("IncludeComponentLineItems", x)
        self.assertNotIn("<IncludeLineItems>", x)

    def test_transfer_has_no_line_flag(self):
        self.assertNotIn("LineItems", qbxml.txn_query("transfer", "2020-01-01", "2020-01-31"))


class GeneralLedger(unittest.TestCase):
    def setUp(self):
        self.idx = gl.AccountIndex((a[1], a[2]) for a in fixtures.ACCOUNTS)
        xml = fixtures.gl_2020_01().encode()
        self.rows = list(gl.parse(xml, self.idx))
        self.lines = [r for r in self.rows if r["kind"] == "line"]
        self.begins = {r["account"]: r["balance_c"] for r in self.rows if r["kind"] == "begin"}

    def placed(self, credit_or_debit):
        return [(r["account"], r["account_src"]) for r in self.lines
                if credit_or_debit in (r["debit_c"], r["credit_c"])]

    def test_beginning_balances(self):
        self.assertEqual(self.begins["Checking"], 100000)
        self.assertEqual(self.begins["Retained Earnings"], -100000)
        self.assertIn("Sales:Retail", self.begins)
        # the aggregate parent header carries no beginning balance of its own,
        # but "Sales - Other" does, and it belongs to Sales
        self.assertEqual(self.begins.get("Sales"), 0)

    def test_section_rules(self):
        self.assertEqual(self.placed(10000), [("Sales:Retail", "section")])
        self.assertEqual(self.placed(5000), [("Sales:Wholesale", "section_child")])
        self.assertEqual(self.placed(500), [("Sales", "section")])

    def test_headerless_rows(self):
        self.assertIn(("Expenses:Freight", "subtotal_proximity"), self.placed(2000))
        self.assertIn(("Shipping:Freight", "subtotal_cascade"), self.placed(700))

    def test_balanced(self):
        self.assertEqual(sum(r["debit_c"] or 0 for r in self.lines),
                         sum(r["credit_c"] or 0 for r in self.lines))


class EndToEnd(unittest.TestCase):
    def build(self, **kw):
        self.tmp = tempfile.TemporaryDirectory()
        cfg = config.load(fixtures.write_archive(self.tmp.name, **kw))
        load.Loader(cfg).build()
        rc = validate.main(cfg)
        status = json.loads((cfg.validation_dir / "status.json").read_text())
        gates = {g["name"]: g for g in status["gates"]}
        return cfg, rc, gates

    def tearDown(self):
        self.tmp.cleanup()

    def test_clean_archive_passes(self):
        cfg, rc, gates = self.build()
        for name in ("plan_coverage", "ledger_balanced", "tieout", "month_continuity",
                     "pnl_net_income", "accounts_placed", "completeness", "line_detail",
                     "no_conflicts", "lists_clean"):
            self.assertEqual(gates[name]["status"], "pass", (name, gates[name]["detail"]))
        self.assertEqual(rc, 0)
        con = sqlite3.connect(cfg.db_path)
        self.assertEqual(con.execute("SELECT fullname FROM dim_list WHERE list_id='V1'")
                         .fetchone()[0], "Freight Co")
        self.assertEqual(con.execute("SELECT COUNT(*) FROM fact_txn WHERE txn_kind='invoice'")
                         .fetchone()[0], 1)
        self.assertEqual(con.execute("SELECT identical FROM load_duplicate").fetchall(), [(1,)])
        self.assertEqual(con.execute("SELECT COUNT(*) FROM xwalk_orphans").fetchone()[0], 0)
        con.close()

    def test_tie_failure_is_caught(self):
        rows = [r if r[0] != "Checking" else ("Checking", "1124.00", None)
                for r in fixtures.TB_2020]
        _, rc, gates = self.build(tb_rows=rows)
        self.assertEqual(gates["tieout"]["status"], "fail")
        self.assertEqual(rc, 1)

    def test_net_income_mismatch_is_caught(self):
        _, rc, gates = self.build(net_income="127.00")
        self.assertEqual(gates["pnl_net_income"]["status"], "fail")
        self.assertEqual(rc, 1)

    def test_collapsed_fiscal_year_is_caught(self):
        _, rc, gates = self.build(collapsed_year=True)
        self.assertEqual(gates["fiscal_year_detail"]["status"], "fail")
        self.assertEqual(rc, 1)

    def test_missing_planned_chunk_is_caught(self):
        _, rc, gates = self.build(skip_chunk="gl_accrual_2020-01")
        self.assertEqual(gates["plan_coverage"]["status"], "fail")
        self.assertEqual(rc, 1)

    def test_duplicate_across_windows_is_not_accepted(self):
        _, rc, gates = self.build(duplicate_across_windows=True)
        self.assertEqual(gates["completeness"]["status"], "fail")
        self.assertEqual(rc, 1)

    def test_missing_documents_are_caught(self):
        _, rc, gates = self.build(probe_invoice=5)
        self.assertEqual(gates["completeness"]["status"], "fail")
        self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()
