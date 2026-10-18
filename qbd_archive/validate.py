"""Acceptance gates. The archive is fit to rely on only when every gate passes.

Most gates compare the archive to numbers QuickBooks reports about itself
(its trial balance, its P&L, its own record counts); the rest check the
archive's internal consistency.

  plan_coverage        every report chunk the period plan calls for is loaded,
                       with the planned dates, and nothing else; every document
                       type finished extracting
  ledger_balanced      every GL chunk: total debits = total credits
  tieout               per period and account: QuickBooks' beginning balance +
                       GL activity = QuickBooks' trial balance, to the cent
  month_continuity     within a fiscal year, each month's beginning balance =
                       the prior month's ending balance, per account
  fiscal_year_detail   no GL chunk hides earlier fiscal years' P&L detail
                       behind "Closing Entry" lines
  pnl_net_income       per period: GL net income by account type = net income
                       on QuickBooks' P&L
  fiscal_year_setting  the configured fiscal year matches the company file's
  accounts_placed      every GL row is placed on an account; the rule used for
                       each row is counted
  completeness         per list and document type: records loaded = QuickBooks'
                       own count; a variance passes only when byte-identical
                       copies inside one response explain it; no hard gaps
  line_detail          documents that should carry lines do
  no_conflicts         no object came back twice with different content
  lists_clean          full names unique within each list
  crosswalk (warn)     ledger rows without a source document, and the reverse
  linked_txns (warn)   document types extracted without linked-txn detail

Artifacts land in validation/: summary.md (human), status.json (machine),
and CSVs for drill-down (named in each gate's detail).
"""
import csv
import datetime as dt
import json
import sqlite3
import time
from collections import defaultdict

from . import qbxml
from .money import fmt

PNL_TYPES = ("Income", "CostOfGoodsSold", "Expense", "OtherIncome", "OtherExpense")
FLAT = ("vendor", "terms", "employee", "othername", "salestaxcode", "paymentmethod")
MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august",
          "september", "october", "november", "december"]
# Document types that always carry line detail when they carry an amount.
LINED_TYPES = ("invoice", "creditmemo", "salesreceipt", "bill", "check", "cccharge",
               "cccredit", "vendorcredit", "itemreceipt", "journal", "deposit",
               "salesorder", "purchaseorder", "estimate", "salestaxpayck")


class Gate:
    def __init__(self, name, assertion):
        self.name, self.assertion = name, assertion
        self.status, self.detail, self.facts = "pass", [], {}

    def fail(self, msg):
        self.status = "fail"
        self.detail.append(msg)

    def warn(self, msg):
        if self.status == "pass":
            self.status = "warn"
        self.detail.append(msg)

    def note(self, msg):
        self.detail.append(msg)


class Validator:
    def __init__(self, cfg):
        self.cfg = cfg
        self.out = cfg.validation_dir
        self.out.mkdir(parents=True, exist_ok=True)
        if not cfg.db_path.exists():
            raise SystemExit(f"no archive database at {cfg.db_path}; run load first")
        self.con = sqlite3.connect(cfg.db_path.resolve().as_uri() + "?mode=ro", uri=True)
        self.gates = []
        self.plan = cfg.report_plan()

    def gate(self, name, assertion):
        g = Gate(name, assertion)
        self.gates.append(g)
        return g

    def q(self, sql, args=()):
        return self.con.execute(sql, args).fetchall()

    def csv(self, name, header, rows):
        with open(self.out / name, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(header)
            w.writerows(rows)

    def gl_chunks(self, basis, period):
        """Planned GL chunk keys for a period, in date order."""
        return [k for k, kind, b, p, frm, to in self.plan
                if kind == "gl" and b == basis and p == period]

    # ---------- gates ----------
    def plan_coverage(self):
        g = self.gate("plan_coverage", "every planned report chunk is loaded with its planned "
                                       "dates; every document type finished extracting")
        loaded = {r[0]: r[1:] for r in self.q(
            "SELECT src_key, kind, basis, period, period_from, period_to FROM source_file "
            "WHERE kind IN ('gl','tb','pnl','bs')")}
        missing, wrong = [], []
        for key, kind, basis, period, frm, to in self.plan:
            got = loaded.get(key)
            if got is None:
                missing.append(key)
            elif tuple(got) != (kind, basis, period, frm, to):
                wrong.append(key)
        extra = sorted(set(loaded) - {p[0] for p in self.plan})
        for k in missing[:20]:
            g.fail(f"planned chunk not loaded: {k}")
        if len(missing) > 20:
            g.fail(f"... {len(missing) - 20} more planned chunks not loaded")
        for k in wrong:
            g.fail(f"{k}: loaded with dates or period that differ from the plan")
        for k in extra:
            g.fail(f"{k}: loaded but not in the plan")
        # GL chunks must cover the timeline with no gap and no overlap
        for basis in self.cfg.bases:
            chunks = sorted((frm, to, k) for k, kind, b, p, frm, to in self.plan
                            if kind == "gl" and b == basis)
            for (f0, t0, k0), (f1, t1, k1) in zip(chunks, chunks[1:]):
                if dt.date.fromisoformat(t0) + dt.timedelta(days=1) != dt.date.fromisoformat(f1):
                    g.fail(f"{basis}: GL chunks {k0} and {k1} leave a gap or overlap")
        types = self.q("SELECT txn_kind, status, gaps FROM extract_type")
        for t, status, gaps in types:
            if status != "complete":
                g.fail(f"document type {t}: extraction {status}; gaps {gaps}")
        g.facts = {"planned": len(self.plan), "missing": len(missing), "extra": len(extra)}

    def ledger_balanced(self):
        g = self.gate("ledger_balanced", "every GL chunk: total debits = total credits")
        rows = self.q("""SELECT src_key, basis, COUNT(*), SUM(COALESCE(debit_c,0)),
                                SUM(COALESCE(credit_c,0)) FROM gl_line GROUP BY 1,2 ORDER BY 1""")
        bad = [r for r in rows if r[3] != r[4]]
        for r in bad:
            g.fail(f"{r[0]}: debits {fmt(r[3])} vs credits {fmt(r[4])}")
        g.facts = {"chunks": len(rows), "lines": sum(r[2] for r in rows),
                   "unbalanced": len(bad)}
        self.csv("ledger_balanced.csv", ["src_key", "basis", "lines", "debits", "credits"],
                 [(r[0], r[1], r[2], fmt(r[3]), fmt(r[4])) for r in rows])

    def _begin(self, basis, key):
        return {a: b or 0 for a, b in self.q(
            "SELECT account_fullname, SUM(balance_c) FROM gl_beginning_balance "
            "WHERE basis=? AND src_key=? GROUP BY 1", (basis, key))}

    def _activity(self, basis, keys):
        if not keys:
            return {}
        qm = ",".join("?" * len(keys))
        return {a: (d or 0, c or 0) for a, d, c in self.q(
            f"SELECT account_fullname, SUM(debit_c), SUM(credit_c) FROM gl_line "
            f"WHERE basis=? AND src_key IN ({qm}) GROUP BY 1", [basis] + keys)}

    def tieout(self):
        g = self.gate("tieout", "per period and account: beginning balance + GL debits - "
                                "credits = trial balance, same basis, to the cent")
        summary = []
        for basis in self.cfg.bases:
            for p in self.cfg.periods():
                chunks = self.gl_chunks(basis, p.label)
                act = self._activity(basis, chunks)
                beg = self._begin(basis, chunks[0]) if chunks else {}
                tb = {a: (d or 0) - (c or 0) for a, d, c in self.q(
                    "SELECT account_fullname, debit_c, credit_c FROM rpt_tb "
                    "WHERE basis=? AND period=?", (basis, p.label))}
                rows, tied, off, off_amt = [], 0, 0, 0
                # An account on only one side ties only if the other side is zero;
                # an empty trial balance (QuickBooks prints just a total row when
                # nothing has a balance) ties only an all-zero ledger.
                for a in sorted(set(act) | set(beg) | set(tb), key=lambda s: s or ""):
                    d, c = act.get(a, (0, 0))
                    b = beg.get(a, 0)
                    computed = b + d - c
                    t = tb.get(a)
                    ok = computed == (t or 0)
                    tied += ok
                    off += not ok
                    if not ok:
                        off_amt += abs(computed - (t or 0))
                    rows.append((a, fmt(b), fmt(d), fmt(c), fmt(computed),
                                 "" if t is None else fmt(t), fmt(computed - (t or 0)), ok))
                self.csv(f"tieout_{basis}_{p.label}.csv",
                         ["account", "beginning", "debits", "credits", "computed",
                          "trial_balance", "diff", "tied"], rows)
                summary.append((basis, p.label, tied, off, "ok" if off == 0 else
                                f"{off} accounts off, |diff| {fmt(off_amt)}"))
                if off:
                    g.fail(f"{basis} {p.label}: {off} accounts do not tie "
                           f"(sum |diff| {fmt(off_amt)}); see tieout_{basis}_{p.label}.csv")
        g.facts = {"periods": [{"basis": s[0], "period": s[1], "tied": s[2], "off": s[3],
                                "result": s[4]} for s in summary]}

    def month_continuity(self):
        g = self.gate("month_continuity", "within each fiscal year, every beginning balance "
                                          "QuickBooks printed = the prior month's ending balance")
        # Walk each fiscal year month by month, carrying every account's
        # running balance from the year's opening. Where QuickBooks printed a
        # beginning balance it must equal the running balance. Where it printed
        # none (a month whose section header is missing from the report), the
        # running balance carries on and the month is counted as unreported.
        rows, unreported = [], 0
        for basis in self.cfg.bases:
            for p in self.cfg.periods():
                chunks = self.gl_chunks(basis, p.label)
                if not chunks:
                    continue
                running = dict(self._begin(basis, chunks[0]))
                for i, key in enumerate(chunks):
                    begin = self._begin(basis, key)
                    if i:
                        for a in set(running) | set(begin):
                            if a in begin:
                                if begin[a] != running.get(a, 0):
                                    rows.append((basis, chunks[i - 1], key, a,
                                                 fmt(running.get(a, 0)), fmt(begin[a]),
                                                 fmt(begin[a] - running.get(a, 0))))
                                running[a] = begin[a]
                            elif running.get(a, 0):
                                unreported += 1
                    for a, (d, c) in self._activity(basis, [key]).items():
                        running[a] = running.get(a, 0) + d - c
        if rows:
            g.fail(f"{len(rows)} account-months where QuickBooks' beginning balance differs "
                   "from the prior month's close; see month_continuity.csv")
        if unreported:
            g.note(f"{unreported:,} account-months with a balance but no beginning balance "
                   "printed by QuickBooks (section header missing from the report); carried "
                   "forward from the prior month")
        g.facts = {"mismatches": len(rows), "unreported": unreported}
        self.csv("month_continuity.csv", ["basis", "chunk", "next_chunk", "account",
                                          "carried_balance", "printed_beginning", "diff"], rows)

    def fiscal_year_detail(self):
        g = self.gate("fiscal_year_detail", "no ledger chunk hides earlier fiscal years' "
                                            "P&L detail behind closing entries")
        rows = self.q("""SELECT src_key, COUNT(*), MIN(txn_date), MAX(txn_date),
                                SUM(COALESCE(debit_c,0)), SUM(COALESCE(credit_c,0))
                         FROM gl_line WHERE txn_type LIKE 'Closing Entry%'
                           AND (COALESCE(debit_c,0) <> 0 OR COALESCE(credit_c,0) <> 0)
                         GROUP BY 1""")
        for key, n, d0, d1, d, c in rows:
            g.fail(f"{key}: {n} nonzero closing entries {d0}..{d1} (debits {fmt(d)}, credits "
                   f"{fmt(c)}): P&L detail for those years is collapsed. Move books.start "
                   f"back to the first fiscal year with activity and re-extract.")

    def pnl_net_income(self):
        g = self.gate("pnl_net_income", "per period: GL net income by account type = "
                                        "net income on QuickBooks' rendered P&L")
        rows = []
        for basis in self.cfg.bases:
            for p in self.cfg.periods():
                keys = self.gl_chunks(basis, p.label)
                qm = ",".join("?" * len(keys)) or "''"
                gl = {t: (d or 0, c or 0) for t, d, c in self.q(f"""
                    SELECT a.account_type, SUM(g.debit_c), SUM(g.credit_c)
                    FROM gl_line g JOIN dim_account a ON a.fullname = g.account_fullname
                    WHERE g.basis=? AND g.src_key IN ({qm})
                      AND a.account_type IN ({",".join("?" * len(PNL_TYPES))})
                    GROUP BY 1""", [basis] + keys + list(PNL_TYPES))}
                ni = sum(c - d for d, c in gl.values())
                stmt_rows = self.q("SELECT COUNT(*) FROM rpt_summary WHERE kind='pnl' "
                                   "AND basis=? AND period=?", (basis, p.label))[0][0]
                stmt = self.q("""SELECT amount_c FROM rpt_summary WHERE kind='pnl' AND basis=?
                                 AND period=? AND lower(label) LIKE 'net income%'
                                 ORDER BY seq DESC LIMIT 1""", (basis, p.label))
                s = stmt[0][0] if stmt else None
                rows.append((basis, p.label, fmt(ni), "" if s is None else fmt(s),
                             fmt(ni - (s or 0))))
                if s is None and stmt_rows:
                    g.fail(f"{basis} {p.label}: the P&L has {stmt_rows} rows but none labeled "
                           f"Net Income, so it can't be compared (GL net income {fmt(ni)})")
                elif ni != (s or 0):
                    g.fail(f"{basis} {p.label}: GL net income {fmt(ni)} vs P&L "
                           f"{fmt(s or 0)}")
        self.csv("pnl_net_income.csv", ["basis", "period", "gl_net_income",
                                        "pnl_net_income", "diff"], rows)

    def fiscal_year_setting(self):
        g = self.gate("fiscal_year_setting", "the configured fiscal year start matches the "
                                             "company file's")
        meta = dict(self.q("SELECT key, value FROM meta"))
        qb = (meta.get("qb_fiscal_year_start_month") or "").strip().lower()
        if not qb:
            g.warn("the company file's fiscal year setting wasn't extracted")
            return
        want = MONTHS[self.cfg.fiscal_year_start_month - 1]
        if qb != want:
            g.fail(f"company file starts its fiscal year in {qb.title()}, config says "
                   f"{want.title()}")

    def accounts_placed(self):
        g = self.gate("accounts_placed", "every GL row is placed on an account")
        by_rule = self.q("SELECT basis, account_src, COUNT(*) FROM gl_line GROUP BY 1,2 ORDER BY 1,2")
        g.facts = {"by_rule": [{"basis": b, "rule": r, "rows": n} for b, r, n in by_rule]}
        un = self.q("""SELECT src_key, src_row, account_src, account_col, txn_type, txn_date,
                              debit_c, credit_c FROM gl_line WHERE account_fullname IS NULL""")
        if un:
            g.fail(f"{len(un)} GL rows could not be placed on an account; "
                   "see accounts_unplaced.csv")
        self.csv("accounts_unplaced.csv", ["src_key", "src_row", "rule", "account_column",
                                           "txn_type", "txn_date", "debit", "credit"],
                 [(a, b, r, c, d, e, fmt(f), fmt(h)) for a, b, r, c, d, e, f, h in un])
        stray = self.q("""SELECT DISTINCT g.account_fullname FROM gl_line g
                          LEFT JOIN dim_account a ON a.fullname = g.account_fullname
                          WHERE g.account_fullname IS NOT NULL AND a.list_id IS NULL""")
        if stray:
            g.warn("GL accounts not in the chart of accounts (QuickBooks system sections "
                   "such as unassigned postings; not counted in pnl_net_income): "
                   + ", ".join(sorted(r[0] for r in stray)[:10]))
        repaired = {r: n for _, r, n in by_rule if r not in ("section", None)}
        if repaired:
            g.note("rows placed from the Account column because the report omitted "
                   "their section header: " + ", ".join(f"{k} {v:,}" for k, v in
                                                        sorted(repaired.items())))

    def completeness(self):
        g = self.gate("completeness", "records loaded = QuickBooks' own count per type; "
                                      "variance only from identical copies inside one "
                                      "response; no hard gaps")
        probe = {r[0]: r for r in self.q("SELECT object_type, query, status_code, ret_count, "
                                         "message FROM probe_count")}
        if not probe:
            g.fail("no count probe in the archive (run extract with the probe phase)")
        same_resp = defaultdict(int)
        for scope, n in self.q("SELECT scope, COUNT(*) FROM load_duplicate "
                               "WHERE identical=1 AND same_response=1 GROUP BY 1"):
            same_resp[scope] = n
        cross = defaultdict(int)
        for scope, n in self.q("SELECT scope, COUNT(*) FROM load_duplicate "
                               "WHERE identical=1 AND same_response=0 GROUP BY 1"):
            cross[scope] = n
        loaded = {f"list:{t}": n for t, n in self.q(
            "SELECT list_type, COUNT(*) FROM dim_list GROUP BY 1")}
        loaded.update({f"txn:{t}": n for t, n in self.q(
            "SELECT txn_kind, COUNT(*) FROM fact_txn GROUP BY 1")})
        extracted = {r[0] for r in self.q("SELECT txn_kind FROM extract_type")}
        configured = set(self.cfg.txn_types or qbxml.TXN_TYPES) - set(self.cfg.exclude_txn_types)
        rows = []
        for obj in sorted(set(probe) | set(loaded)):
            scope, tag = obj.split(":", 1)
            p = probe.get(obj)
            want = p[3] if p else None
            have = loaded.get(obj, 0)
            dup = same_resp.get(obj, 0)
            verdict = "ok"
            if p is None:
                verdict = "no probe"
                g.fail(f"{obj}: {have:,} loaded but QuickBooks was never asked for a count")
            elif p[2] not in ("0", "1"):
                verdict = f"probe error {p[2]}"
                if have or (scope == "txn" and tag in configured):
                    g.fail(f"{obj}: QuickBooks' count failed ({p[4]}); {have:,} loaded, "
                           "unverified. If this edition doesn't support the type, exclude "
                           "it in config.")
                else:
                    g.note(f"{obj}: count not available ({p[4]}); not extracted")
            elif scope == "txn" and tag not in extracted and want:
                verdict = "NOT EXTRACTED"
                msg = f"{obj}: {want:,} in QuickBooks but not extracted"
                if tag in self.cfg.exclude_txn_types:
                    g.warn(msg + " (excluded in config)")
                else:
                    g.fail(msg)
            elif have == want:
                pass
            elif have + dup == want:
                verdict = f"ok ({dup:,} identical copies inside single responses)"
            else:
                verdict = f"VARIANCE {have - want:+,}"
                g.fail(f"{obj}: loaded {have:,} vs QuickBooks count {want:,} "
                       f"(identical copies inside one response: {dup:,}; across "
                       f"responses: {cross.get(obj, 0):,})")
            rows.append((obj, "" if want is None else want, have, dup, cross.get(obj, 0),
                         verdict))
        self.csv("completeness.csv", ["object", "quickbooks_count", "loaded",
                                      "copies_same_response", "copies_across_responses",
                                      "verdict"], rows)

    def line_detail(self):
        g = self.gate("line_detail", "documents that carry an amount carry their lines")
        qm = ",".join("?" * len(LINED_TYPES))
        rows = self.q(f"""SELECT t.txn_kind, COUNT(*) FROM fact_txn t
                          WHERE t.txn_kind IN ({qm}) AND COALESCE(t.amount_c,0) <> 0
                            AND NOT EXISTS (SELECT 1 FROM fact_txn_line l WHERE l.txn_id=t.txn_id)
                          GROUP BY 1""", LINED_TYPES)
        for kind, n in rows:
            g.fail(f"{kind}: {n:,} documents with an amount but no lines (line items "
                   "missing from the response?)")

    def no_conflicts(self):
        g = self.gate("no_conflicts", "no object came back twice with different content")
        rows = self.q("SELECT scope, object_id, src_key, kept_src_key FROM load_duplicate "
                      "WHERE identical=0")
        if rows:
            g.fail(f"{len(rows)} objects returned with differing content; "
                   "see conflicts.csv")
        self.csv("conflicts.csv", ["scope", "object_id", "src_key", "kept_src_key"], rows)

    def lists_clean(self):
        g = self.gate("lists_clean", "full names unique within each list; flat lists "
                                     "named by their own Name")
        rows = self.q("""SELECT list_type, fullname, COUNT(*) FROM dim_list
                         WHERE fullname IS NOT NULL GROUP BY 1,2 HAVING COUNT(*) > 1""")
        for t, f, n in rows[:20]:
            g.fail(f"{t}: {f!r} appears {n} times")
        blank = self.q("SELECT list_type, COUNT(*) FROM dim_list WHERE fullname IS NULL "
                       "OR fullname='' GROUP BY 1")
        for t, n in blank:
            g.fail(f"{t}: {n} records without a name")
        qm = ",".join("?" * len(FLAT))
        polluted = self.q(f"SELECT list_type, COUNT(*) FROM dim_list WHERE list_type IN ({qm}) "
                          f"AND fullname <> name GROUP BY 1", FLAT)
        for t, n in polluted:
            g.fail(f"{t}: {n} records whose name came from a nested reference")

    def crosswalk(self):
        g = self.gate("crosswalk", "ledger rows and source documents join on TxnNumber "
                                   "(residue explained by type)")
        rows = self.q("""SELECT side, txn_type, COUNT(*), SUM(gl_lines), SUM(amount_c)
                         FROM xwalk_orphans GROUP BY 1,2 ORDER BY 1, 3 DESC""")
        for side, t, n, lines, amt in rows:
            g.warn(f"{side} {t}: {n:,} transactions ({fmt(amt)})")
        self.csv("crosswalk_orphans.csv", ["side", "type", "transactions", "gl_lines",
                                           "amount"], [(a, b, c, d, fmt(e)) for a, b, c, d, e in rows])

    def linked_txns(self):
        g = self.gate("linked_txns", "linked-transaction detail present where requested")
        for (t,) in self.q("SELECT txn_kind FROM extract_type WHERE linked_txns=0"):
            if t in qbxml.LINKED_TXNS:
                g.warn(f"{t}: extracted without IncludeLinkedTxns (rejected by "
                       "QuickBooks or not requested); its links come only from the other "
                       "side of each link")

    # ---------- run ----------
    def run(self):
        for fn in (self.plan_coverage, self.ledger_balanced, self.tieout,
                   self.month_continuity, self.fiscal_year_detail, self.pnl_net_income,
                   self.fiscal_year_setting, self.accounts_placed, self.completeness,
                   self.line_detail, self.no_conflicts, self.lists_clean, self.crosswalk,
                   self.linked_txns):
            fn()
        status = "fail" if any(g.status == "fail" for g in self.gates) else "pass"
        meta = dict(self.q("SELECT key, value FROM meta"))
        doc = {"status": status, "validated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
               "database": self.cfg.db_path.name, "meta": meta,
               "gates": [{"name": g.name, "assertion": g.assertion, "status": g.status,
                          "detail": g.detail, "facts": g.facts} for g in self.gates]}
        (self.out / "status.json").write_text(json.dumps(doc, indent=1))
        (self.out / "summary.md").write_text(self.markdown(doc), encoding="utf-8")
        print(self.markdown(doc))
        return 0 if status == "pass" else 1

    def markdown(self, doc):
        lines = [f"# Validation: {doc['status'].upper()}", "",
                 f"Database `{doc['database']}`, validated {doc['validated_at']}.", "",
                 "| Gate | Result | Assertion |", "|---|---|---|"]
        for g in doc["gates"]:
            lines.append(f"| {g['name']} | {g['status']} | {g['assertion']} |")
        for g in doc["gates"]:
            if g["name"] == "tieout":
                lines += ["", "## Tie-out by period", "", "| Basis | Period | Accounts tied | Off | Result |",
                          "|---|---|---|---|---|"]
                for p in g["facts"].get("periods", []):
                    lines.append(f"| {p['basis']} | {p['period']} | {p['tied']} | {p['off']} | {p['result']} |")
            if g["detail"]:
                lines += ["", f"## {g['name']}", ""] + [f"- {d}" for d in g["detail"][:60]]
                if len(g["detail"]) > 60:
                    lines.append(f"- ... {len(g['detail']) - 60} more")
        return "\n".join(lines) + "\n"


def main(cfg):
    v = Validator(cfg)
    try:
        return v.run()
    finally:
        v.con.close()
