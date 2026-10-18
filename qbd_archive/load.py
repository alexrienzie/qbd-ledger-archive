"""Build the SQLite archive from raw/ and the manifest.

The database is a pure function of the raw corpus: `load` always rebuilds
from scratch into a temporary file and swaps it in only when the build
finishes. Fixing a parser bug means fixing the code and reloading, never
patching rows in place, so every number in the database can be regenerated
from the qbXML QuickBooks returned.
"""
import hashlib
import json
import os
import sqlite3
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from . import __version__, gl, qbxml
from .manifest import Manifest
from .money import cents

HERE = Path(__file__).parent

ENTITY_REFS = ["CustomerRef", "VendorRef", "EntityRef", "PayeeEntityRef"]
ACCOUNT_REFS = ["ARAccountRef", "APAccountRef", "BankAccountRef", "CreditCardAccountRef",
                "DepositToAccountRef", "AccountRef", "TransferFromAccountRef"]
AMOUNT_FIELDS = ["TotalAmount", "Amount", "AmountDue", "Subtotal", "TotalAmt", "DepositTotal"]

# The document total lives in a different element per type. Invoices carry no
# total at all (Subtotal + SalesTaxTotal); vendor credits use CreditAmount.
# Types not listed fall back to AMOUNT_FIELDS; journal entries total their
# debit lines; assemblies, adjustments and inventory transfers have no total.
AMOUNT_BY_TYPE = {
    "invoice": ("Subtotal", "SalesTaxTotal"),
    "vendorcredit": ("CreditAmount",),
    "bill": ("AmountDue",),
    "deposit": ("DepositTotal",),
    "creditmemo": ("TotalAmount",), "salesreceipt": ("TotalAmount",),
    "recvpayment": ("TotalAmount",), "itemreceipt": ("TotalAmount",),
    "salesorder": ("TotalAmount",), "purchaseorder": ("TotalAmount",),
    "estimate": ("TotalAmount",), "arrefundcc": ("TotalAmount",),
    "check": ("Amount",), "cccharge": ("Amount",), "cccredit": ("Amount",),
    "billpayck": ("Amount",), "billpaycc": ("Amount",), "transfer": ("Amount",),
    "salestaxpayck": ("Amount",), "charge": ("Amount",),
    "buildassembly": (), "invadjust": (), "transferinventory": (), "journal": (),
}

# Header field overrides where the generic order picks the wrong element.
HEADER_OVERRIDES = {
    "buildassembly": {"entity": ["ItemInventoryAssemblyRef"], "account": [], "amount": []},
    "arrefundcc": {"entity": ["CustomerRef"], "account": ["RefundFromAccountRef"]},
    "salestaxpayck": {"entity": ["PayeeEntityRef"], "account": ["BankAccountRef"]},
    "transferinventory": {"entity": ["FromInventorySiteRef"], "account": []},
}


def _doc_amount(tag, ret):
    fields = AMOUNT_BY_TYPE.get(tag)
    if fields is None:
        return _first_amount(ret, AMOUNT_FIELDS)
    vals = [cents(ret.findtext(f)) for f in fields]
    if not fields or all(v is None for v in vals):
        return None
    return sum(v or 0 for v in vals)

# Flat list types have no FullName of their own. Reading "the first
# <FullName> in the record" picks up a nested reference instead (a vendor's
# VendorTypeRef, a TaxVendorRef) and silently renames the record.
FLAT_LIST_TYPES = {"vendor", "terms", "employee", "othername", "salestaxcode",
                   "paymentmethod"}


def log(*a):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), *a, flush=True)


def _ref(el, name):
    r = el.find(name)
    if r is None:
        return None, None
    return r.findtext("ListID"), r.findtext("FullName")


def _first_ref(el, names):
    for n in names:
        lid, fn = _ref(el, n)
        if lid or fn:
            return lid, fn
    return None, None


def _first_amount(el, names):
    for n in names:
        v = cents(el.findtext(n))
        if v is not None:
            return v
    return None


def _canon(el):
    return ET.tostring(el, encoding="unicode")


class Loader:
    def __init__(self, cfg):
        self.cfg = cfg
        self.m = Manifest(cfg.manifest_path)
        self.raw = cfg.raw_dir

    def _file(self, key):
        c = self.m.chunk(key)
        return self.raw / (c.get("file") or f"{key}.xml")

    def _root(self, key):
        return ET.parse(self._file(key)).getroot()

    # ---------- entry point ----------
    def build(self):
        target = self.cfg.db_path
        tmp = target.with_suffix(".building")
        if tmp.exists():
            tmp.unlink()
        con = sqlite3.connect(tmp)
        con.execute("PRAGMA journal_mode=OFF")
        con.execute("PRAGMA synchronous=OFF")
        con.executescript((HERE / "schema.sql").read_text())
        t0 = time.time()
        self.sources(con)
        self.lists(con)
        self.accounts(con)
        self.reports(con)
        self.txns(con)
        self.evidence(con)
        con.executescript((HERE / "views.sql").read_text())
        meta = {"tool_version": __version__, "loaded_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "books_start": self.cfg.books_start.isoformat(),
                "books_end": self.cfg.books_end.isoformat(),
                "bases": ",".join(self.cfg.bases), "company_label": self.cfg.company_label,
                "fiscal_year_start_month": str(self.cfg.fiscal_year_start_month)}
        meta.update(self.company_info())
        con.executemany("INSERT INTO meta VALUES(?,?)", sorted(meta.items()))
        con.commit()
        con.execute("ANALYZE")
        con.close()
        os.replace(tmp, target)
        log(f"loaded {target} in {(time.time() - t0) / 60:.1f} min")
        return target

    def planned(self):
        """Report chunks the current plan calls for, keyed by manifest key.
        Chunks from an earlier plan (a different books.start, say) stay in
        raw/ but are never loaded."""
        plan = {k: (kind, basis, period, frm, to)
                for k, kind, basis, period, frm, to in self.cfg.report_plan()}
        stale = [k for k, c in self.m.data["chunks"].items()
                 if c.get("kind") in ("gl", "tb", "pnl", "bs") and k not in plan]
        if stale:
            log(f"ignoring {len(stale)} report chunks outside the current plan "
                f"(first: {stale[:3]})")
        return plan

    def sources(self, con):
        rows = []
        plan = self.planned()
        for key, c in self.m.data["chunks"].items():
            if c.get("kind") in ("gl", "tb", "pnl", "bs") and key not in plan:
                continue
            rows.append((key, c.get("kind"), c.get("basis"), c.get("period") or c.get("tag"),
                         c.get("from"), c.get("to"), c.get("file"), c.get("sha256"),
                         c.get("bytes"), c.get("pulled_at")))
        con.executemany("INSERT INTO source_file VALUES(?,?,?,?,?,?,?,?,?,?)", rows)

    def company_info(self):
        out = {}
        if self.m.ok("single_company"):
            ret = self._root("single_company").find(".//CompanyRet")
            if ret is not None:
                out["qb_company_name"] = ret.findtext("CompanyName") or ""
                out["qb_fiscal_year_start_month"] = ret.findtext("FirstMonthFiscalYear") or ""
        return out

    # ---------- lists ----------
    def lists(self, con):
        seen = {}
        chunks = self.m.data["chunks"]
        for tag in qbxml.LISTS:
            keys = sorted(k for k, c in chunks.items() if c.get("kind") == "list"
                          and c.get("tag") == tag and c.get("status") == "ok")
            if not keys:
                log(f"list {tag}: not extracted")
                continue
            n = 0
            for key in keys:
                rs = self._root(key).find(f".//{qbxml.LISTS[tag].replace('Rq', 'Rs')}")
                n += self._list_records(con, tag, key, rs, seen)
            log(f"list {tag}: {n:,} records")

    def _list_records(self, con, tag, key, rs, seen):
        n = 0
        if rs is not None:
            for rec in list(rs):
                lid = rec.findtext("ListID")
                if lid is None:
                    continue
                own_full = rec.findtext("FullName")  # direct child only
                name = rec.findtext("Name")
                fullname = name if tag in FLAT_LIST_TYPES else (own_full or name)
                parent, _ = _ref(rec, "ParentRef")
                row = (tag, lid, name, fullname, rec.findtext("IsActive"), parent, rec.tag,
                       rec.findtext("TimeCreated"), rec.findtext("TimeModified"),
                       rec.findtext("EditSequence"), _canon(rec), key)
                prior = seen.get((tag, lid))
                if prior is not None:
                    identical = prior[1] == row[:10]
                    con.execute("INSERT INTO load_duplicate VALUES(?,?,?,?,?,?)",
                                (f"list:{tag}", lid, key, prior[0], int(identical),
                                 int(key == prior[0])))
                    continue
                seen[(tag, lid)] = (key, row[:10])
                con.execute("INSERT INTO dim_list VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", row)
                n += 1
        return n

    def accounts(self, con):
        for lid, name, fullname, active, parent, xml in con.execute(
                "SELECT list_id, name, fullname, is_active, parent_listid, xml "
                "FROM dim_list WHERE list_type='account'").fetchall():
            el = ET.fromstring(xml)
            con.execute("INSERT INTO dim_account VALUES(?,?,?,?,?,?,?,?,?)",
                        (lid, name, fullname, el.findtext("AccountType"),
                         el.findtext("SpecialAccountType"), el.findtext("AccountNumber"),
                         parent, active, int(el.findtext("Sublevel") or 0)))
        for lid, fullname, tag, xml in con.execute(
                "SELECT list_id, fullname, record_tag, xml FROM dim_list "
                "WHERE list_type='item'").fetchall():
            el = ET.fromstring(xml)

            def anywhere(ref):
                r = el.find(f".//{ref}")
                return (r.findtext("ListID"), r.findtext("FullName")) if r is not None else (None, None)
            sop = el.find("SalesOrPurchase")
            gen = _ref(sop, "AccountRef") if sop is not None else (None, None)
            refs = [anywhere("IncomeAccountRef"), anywhere("COGSAccountRef"),
                    anywhere("AssetAccountRef"), anywhere("ExpenseAccountRef"), gen]
            con.execute("INSERT INTO map_item_account VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (lid, fullname, tag) + tuple(x for r in refs for x in r))

    # ---------- reports ----------
    def reports(self, con):
        index = gl.AccountIndex(con.execute("SELECT name, fullname FROM dim_account"))
        plan = self.planned()
        if not index.fullnames:
            log("WARNING: no chart of accounts loaded; GL rows without a section "
                "header cannot be placed")
        chunks = self.m.data["chunks"]
        for key in sorted(k for k, c in chunks.items() if k in plan and c.get("kind") == "gl"
                          and c.get("status") == "ok"):
            c = chunks[key]
            basis, frm = c["basis"], c["from"]
            lines = begins = 0
            batch = []
            for r in gl.parse(str(self._file(key)), index):
                if r["kind"] == "begin":
                    con.execute("INSERT INTO gl_beginning_balance VALUES(?,?,?,?,?,?)",
                                (basis, r["account"], frm, r["balance_c"], key, r["row"]))
                    begins += 1
                    continue
                batch.append((basis, r["txn_type"], r["txn_date"], r["ref_number"],
                              r["txn_number"], r["name"], r["memo"], r["account"],
                              r["account_src"], r["account_col"], r["debit_c"],
                              r["credit_c"], r["running_balance_c"], key, r["row"]))
            con.executemany(
                "INSERT INTO gl_line(basis,txn_type,txn_date,ref_number,txn_number,name,memo,"
                "account_fullname,account_src,account_col,debit_c,credit_c,running_balance_c,"
                "src_key,src_row) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", batch)
            lines = len(batch)
            log(f"{key}: {lines:,} lines, {begins:,} beginning balances")
        for key in sorted(k for k, c in chunks.items() if k in plan and c.get("kind") == "tb"
                          and c.get("status") == "ok"):
            self._tb(con, key, chunks[key])
        for key in sorted(k for k, c in chunks.items() if k in plan and c.get("kind") in ("pnl", "bs")
                          and c.get("status") == "ok"):
            self._summary(con, key, chunks[key])

    def _report(self, key):
        rep = self._root(key).find(".//ReportRet")
        colmap = {}
        for cd in rep.findall("ColDesc"):
            for ct in cd.findall("ColTitle"):
                if ct.get("value"):
                    colmap[ct.get("value")] = cd.get("colID")
        rd = rep.find("ReportData")
        return colmap, (list(rd) if rd is not None else [])

    def _tb(self, con, key, c):
        colmap, nodes = self._report(key)
        n = 0
        for node in nodes:
            if node.tag != "DataRow":
                continue
            v = {x.get("colID"): x.get("value") for x in node.findall("ColData")}
            label = v.get("1")
            d, cr = cents(v.get(colmap.get("Debit", "?"))), cents(v.get(colmap.get("Credit", "?")))
            if label and (d is not None or cr is not None):
                con.execute("INSERT INTO rpt_tb VALUES(?,?,?,?,?,?,?,?)",
                            (c["basis"], c["period"], c["from"], c["to"], label,
                             d or 0, cr or 0, key))
                n += 1

    def _summary(self, con, key, c):
        _, nodes = self._report(key)
        seq = 0
        for node in nodes:
            if node.tag not in ("DataRow", "SubtotalRow", "TotalRow"):
                continue
            v = {x.get("colID"): x.get("value") for x in node.findall("ColData")}
            label = v.get("1")
            amt = None
            for cid in sorted((k for k in v if k != "1"), key=int):
                a = cents(v[cid])
                if a is not None:
                    amt = a  # the last numeric column is the period total
            if label and amt is not None:
                seq += 1
                con.execute("INSERT INTO rpt_summary VALUES(?,?,?,?,?,?,?,?)",
                            (c["kind"], c["basis"], c["period"], seq, node.tag, label, amt, key))

    # ---------- source documents ----------
    def txns(self, con):
        chunks = self.m.data["chunks"]
        for tag in qbxml.TXN_TYPES:
            keys = sorted(k for k, c in chunks.items() if c.get("kind") == "txn"
                          and c.get("tag") == tag and c.get("status") == "ok")
            if not keys:
                continue
            ret_name = qbxml.TXN_TYPES[tag][1]
            kept = {}
            stats = {"docs": 0, "lines": 0, "links": 0, "dups": 0}
            for key in keys:
                for ret in self._root(key).iter(ret_name):
                    tid = ret.findtext("TxnID")
                    if tid is None:
                        continue
                    canon = hashlib.sha1(_canon(ret).encode("utf-8")).digest()
                    if tid in kept:
                        k0, c0 = kept[tid]
                        con.execute("INSERT INTO load_duplicate VALUES(?,?,?,?,?,?)",
                                    (f"txn:{tag}", tid, key, k0, int(canon == c0),
                                     int(key == k0)))
                        stats["dups"] += 1
                        continue
                    kept[tid] = (key, canon)
                    self._doc(con, tag, ret, key, stats)
            del kept
            log(f"{tag}: {stats['docs']:,} documents, {stats['lines']:,} lines, "
                f"{stats['links']:,} links, {stats['dups']:,} duplicate emissions")

    def _doc(self, con, tag, ret, key, stats):
        tid = ret.findtext("TxnID")
        ov = HEADER_OVERRIDES.get(tag, {})
        e_l, e_f = _first_ref(ret, ov.get("entity", ENTITY_REFS))
        a_l, a_f = _first_ref(ret, ov.get("account", ACCOUNT_REFS))
        amt = _doc_amount(tag, ret)
        con.execute("INSERT INTO fact_txn VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (tid, tag, ret.tag, ret.findtext("TxnNumber"), ret.findtext("TxnDate"),
                     ret.findtext("RefNumber"), e_l, e_f, a_l, a_f, amt,
                     ret.findtext("Memo"), ret.findtext("IsPending"),
                     ret.findtext("TimeCreated"), ret.findtext("TimeModified"),
                     ret.findtext("EditSequence"),
                     cents(ret.findtext("Subtotal")), cents(ret.findtext("SalesTaxTotal")),
                     ret.findtext("SalesTaxPercentage"), _ref(ret, "ItemSalesTaxRef")[1],
                     _ref(ret, "CustomerSalesTaxCodeRef")[1],
                     ret.findtext("ShipAddress/State"), ret.findtext("ShipAddress/PostalCode"),
                     ret.findtext("ShipAddress/Country"), ret.findtext("BillAddress/State"),
                     key))
        stats["docs"] += 1
        seq = 0

        def line(line_id, line_tag, item, descr, qty, rate, amount, acct=(None, None),
                 cls=None, entity=None):
            nonlocal seq
            seq += 1
            synthetic = 0
            if line_id is None:
                line_id, synthetic = f"{tid}-L{seq}", 1
            con.execute("INSERT INTO fact_txn_line VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (tid, line_id, synthetic, seq, line_tag, item[0], item[1], descr,
                         qty, rate, amount, acct[0], acct[1], cls, entity))
            stats["lines"] += 1

        def link(linked_id, linked_type, kind, amount):
            con.execute("INSERT INTO fact_txn_link VALUES(?,?,?,?,?)",
                        (tid, linked_id, linked_type, kind, amount))
            stats["links"] += 1

        if tag == "buildassembly":
            # Pending builds move nothing; header only.
            if (ret.findtext("IsPending") or "").lower() == "true":
                return
            line(f"{tid}-OUT", "AssemblyOutput", _ref(ret, "ItemInventoryAssemblyRef"),
                 None, ret.findtext("QuantityToBuild"), None, None)
            for i, ln in enumerate(ret.findall("ComponentItemLineRet"), 1):
                line(f"{tid}-C{i}", ln.tag, _ref(ln, "ItemRef"), ln.findtext("Desc"),
                     ln.findtext("QuantityNeeded"), None, None)
            return

        if tag == "charge":
            # single-line document: the item, quantity and amount sit on the header
            line(None, "ChargeRet", _ref(ret, "ItemRef"), ret.findtext("Desc"),
                 ret.findtext("Quantity"), ret.findtext("Rate"), cents(ret.findtext("Amount")),
                 cls=_ref(ret, "ClassRef")[1])

        def generic(ln):
            if ln.tag == "TransferInventoryLineRet":
                # moves stock between sites; no value, no ledger effect on quantity in total
                line(ln.findtext("TxnLineID"), ln.tag, _ref(ln, "ItemRef"), None,
                     ln.findtext("QuantityTransferred"), None, None)
                return
            if ln.tag == "InventoryAdjustmentLineRet":
                # Adjustments carry the change, not a quantity and amount.
                line(ln.findtext("TxnLineID"), ln.tag, _ref(ln, "ItemRef"), None,
                     ln.findtext("QuantityDifference"), None,
                     cents(ln.findtext("ValueDifference")))
                return
            if ln.tag == "SalesTaxPaymentCheckLineRet":
                line(ln.findtext("TxnLineID"), ln.tag, _ref(ln, "ItemSalesTaxRef"), None,
                     None, None, cents(ln.findtext("Amount")))
                return
            _, cls = _ref(ln, "ClassRef")
            _, ent = _ref(ln, "EntityRef")
            line(ln.findtext("TxnLineID"), ln.tag, _ref(ln, "ItemRef"),
                 ln.findtext("Desc") or ln.findtext("Memo"), ln.findtext("Quantity"),
                 ln.findtext("Rate") or ln.findtext("Cost"), cents(ln.findtext("Amount")),
                 _ref(ln, "AccountRef"), cls, ent)
            if ln.tag == "DepositLineRet" and ln.findtext("TxnID"):
                # a deposit line that sweeps a received payment from undeposited funds
                link(ln.findtext("TxnID"), ln.findtext("TxnType"), "deposited",
                     cents(ln.findtext("Amount")))

        for ln in list(ret):
            t = ln.tag
            if t.endswith("LineGroupRet"):
                for sub in ln:
                    if sub.tag.endswith("LineRet"):
                        generic(sub)
            elif t.endswith("LineRet") or t in ("JournalDebitLine", "JournalCreditLine"):
                generic(ln)
            elif t == "AppliedToTxnRet":
                link(ln.findtext("TxnID"), ln.findtext("TxnType"), "applied",
                     cents(ln.findtext("Amount")))
            elif t == "RefundAppliedToTxnRet":
                link(ln.findtext("TxnID"), ln.findtext("TxnType"), "refund_applied",
                     cents(ln.findtext("RefundAmount")))
            elif t == "LinkedTxn":
                link(ln.findtext("TxnID"), ln.findtext("TxnType"),
                     ln.findtext("LinkType") or "linked", cents(ln.findtext("Amount")))
        if tag == "journal":
            con.execute("""UPDATE fact_txn SET amount_c = (SELECT SUM(amount_c) FROM fact_txn_line
                           WHERE txn_id=? AND line_tag='JournalDebitLine') WHERE txn_id=?""",
                        (tid, tid))

    # ---------- evidence ----------
    def evidence(self, con):
        by_rq = {v.replace("Rq", "Rs"): ("list", k) for k, v in qbxml.LISTS.items()}
        by_rq.update({v[0].replace("Rq", "Rs"): ("txn", k)
                      for k, v in qbxml.TXN_TYPES.items()})
        probes = sorted(k for k, c in self.m.data["chunks"].items()
                        if c.get("kind") == "probe" and c.get("status") == "ok")
        for key in probes:
            root = self._root(key)
            for rs in root.iter():
                if rs.tag in by_rq:
                    scope, tag = by_rq[rs.tag]
                    rc = rs.get("retCount")
                    con.execute("INSERT INTO probe_count VALUES(?,?,?,?,?)",
                                (f"{scope}:{tag}", rs.tag, rs.get("statusCode"),
                                 int(rc) if rc not in (None, "") else None,
                                 rs.get("statusMessage")))
        for key, c in self.m.data["chunks"].items():
            if c.get("kind") == "probe" and c.get("status") == "failed":
                con.execute("INSERT INTO probe_count VALUES(?,?,?,?,?)",
                            (c.get("object"), None, "failed", None, c.get("error")))
        for tag, st in self.m.data["types"].items():
            con.execute("INSERT INTO extract_type VALUES(?,?,?,?,?)",
                        (tag, st.get("status"), len(st.get("windows", [])),
                         json.dumps(st.get("gaps", [])), int(bool(st.get("linked_txns")))))


def main(cfg):
    Loader(cfg).build()
    return 0
