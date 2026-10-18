"""The extraction engine: pulls every report, list and source document into
raw/ as verbatim qbXML, checkpointed in the manifest.

Rules this engine enforces (each one cost real time in the build it came from):

* Completeness over speed. A document query that times out is split into
  smaller date windows, down to a single day, with line items kept. Detail is
  never dropped to make a query finish. A window that still can't be pulled
  is recorded as a hard gap, and validation fails until it's resolved.
* Every response is checked against its own request (a unique requestID per
  request, the response element, the report title, dates inside the window)
  before it's accepted. On a shared bridge, another caller's response can
  land under your request's name.
* Basis is explicit on every report.
* No iterators. qbXML iterators live inside one SDK session; any transport
  that opens a session per request breaks them. Date windows work everywhere.
* QuickBooks is never force-quit. Stalls are waited out; a persistent
  blocker (modal dialog, revoked access grant) trips a circuit breaker and
  the run stops for a person to clear it. The manifest makes restarts free.
"""
import datetime as dt
import hashlib
import os
import re
import sys
import threading
import time
import uuid
import xml.etree.ElementTree as ET
import zlib

from . import qbxml
from .config import EARLIEST, LATEST
from .manifest import Manifest
from .money import norm_date
from .transport import TransportError

AUTH_ERRORS = ("re-authorize", "not authorized", "access denied", "denied access",
               "not allowed to log", "does not have permission", "permission to access")


def log(*a):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), *a, flush=True)


class CircuitOpen(Exception):
    pass


def _is_timeout(err):
    return "timeout" in (err or "").lower() or "timed out" in (err or "").lower()


class Extractor:
    def __init__(self, cfg, transport):
        self.cfg = cfg
        self.t = transport
        self.m = None  # loaded after the lock is taken
        cfg.raw_dir.mkdir(parents=True, exist_ok=True)
        cfg.request_dir.mkdir(parents=True, exist_ok=True)
        self.consecutive_failures = 0
        self.lock_path = cfg.archive_dir / "extract.lock"
        self.token = uuid.uuid4().hex
        self._stop = threading.Event()
        # A lock untouched for longer than any single request can take is stale.
        longest = max(cfg.report_timeout, cfg.txn_timeout, cfg.list_timeout, 900)
        self.stale_after = 2 * longest + 600

    # ---------- locking ----------
    def __enter__(self):
        try:
            fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            age = time.time() - self.lock_path.stat().st_mtime
            if age < self.stale_after:
                raise SystemExit(f"another extractor holds {self.lock_path} (touched "
                                 f"{age:.0f}s ago); one driver per archive. If no extractor "
                                 "is running, delete the lock file.")
            log(f"breaking stale lock ({age:.0f}s old)")
            self.lock_path.unlink(missing_ok=True)
            fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, f"{self.token} pid {os.getpid()} {time.strftime('%Y-%m-%dT%H:%M:%S')}\n"
                 .encode())
        os.close(fd)
        # Heartbeat on a thread, so a long request keeps the lock fresh.
        threading.Thread(target=self._heartbeat, daemon=True).start()
        self.m = Manifest(self.cfg.manifest_path)  # read only once we own the archive
        return self

    def _heartbeat(self):
        while not self._stop.wait(30):
            try:
                os.utime(self.lock_path)
            except FileNotFoundError:
                return

    def __exit__(self, *exc):
        self._stop.set()
        try:
            if self.lock_path.read_text().startswith(self.token):
                self.lock_path.unlink()
        except FileNotFoundError:
            pass

    # ---------- one request ----------
    @staticmethod
    def request_id(key):
        return str(zlib.crc32(key.encode()) % 900_000_000 + 100_000_000)

    def _send(self, key, body, timeout, on_error="stopOnError"):
        body = body.replace('requestID="1"', f'requestID="{self.request_id(key)}"', 1)
        xml = qbxml.envelope(body, self.cfg.qbxml_version, on_error)
        (self.cfg.request_dir / f"{key}.xml").write_text(xml, encoding="utf-8")
        return xml, self.t.send(xml, timeout)

    def pull(self, key, body, checker, timeout, attempts=3, meta=None,
             on_error="stopOnError", retry_timeouts=True):
        """Send, validate, store. Returns (root, None) or (None, error)."""
        err = None
        rid = self.request_id(key)
        for attempt in range(1, attempts + 1):
            try:
                req_xml, resp = self._send(key, body, timeout, on_error)
            except TransportError as e:
                err = str(e)
            else:
                try:
                    root = ET.fromstring(resp)
                except ET.ParseError as e:
                    err = f"response is not XML: {e}"
                else:
                    rs = next((el for el in root.iter() if el.get("statusCode") is not None),
                              None)
                    if rs is not None and rs.get("requestID") not in (None, rid):
                        err = (f"response is for requestID {rs.get('requestID')}, expected "
                               f"{rid} (collision?)")
                    else:
                        err = checker(root)
                    if err is None:
                        self._store(key, req_xml, resp, meta or {})
                        return root, None
                    if err.startswith("QBSTATUS"):
                        return None, err  # QuickBooks said no; retrying won't change it
            if "parsing the provided XML" in err:
                return None, err  # request schema error: deterministic
            if _is_timeout(err) and not retry_timeouts:
                return None, err  # the caller splits the window instead
            log(f"  {key} attempt {attempt}: {err}")
            if attempt < attempts:
                time.sleep(self._backoff(err))
        return None, err

    @staticmethod
    def _backoff(err):
        e = err.lower()
        if "modal dialog" in e:
            return 90       # someone has to click it; give them a chance
        if "could not start quickbooks" in e:
            return 30       # a launch during the previous instance's shutdown
        if "beginsession" in e or "multiple instances" in e:
            return 30
        if _is_timeout(e):
            return 150      # QuickBooks may still be chewing the abandoned query
        return 20

    def _store(self, key, req_xml, resp, meta):
        path = self.cfg.raw_dir / f"{key}.xml"
        data = resp.encode("utf-8")
        tmp = path.with_suffix(".part")
        tmp.write_bytes(data)
        os.replace(tmp, path)
        self.m.put_chunk(key, status="ok", file=path.name, bytes=len(data),
                         sha256=hashlib.sha256(data).hexdigest(),
                         request_sha256=hashlib.sha256(req_xml.encode()).hexdigest(),
                         **meta)

    def _result(self, key, ok, err=None):
        if ok:
            self.consecutive_failures = 0
            return
        self.consecutive_failures += 1
        log(f"{key}: FAILED ({self.consecutive_failures} in a row): {err}")
        if any(s in (err or "").lower() for s in AUTH_ERRORS):
            raise CircuitOpen("QuickBooks revoked or never granted this application's "
                              "access. Re-approve it in QuickBooks (Edit > Preferences > "
                              "Integrated Applications), then rerun.")
        if self.consecutive_failures >= self.cfg.max_consecutive_failures:
            raise CircuitOpen(f"{self.consecutive_failures} consecutive failures: "
                              "likely a persistent blocker on the QuickBooks machine "
                              "(modal dialog, QuickBooks closed, file in use). Clear it "
                              "and rerun; completed chunks are kept.")

    # ---------- response checks ----------
    @staticmethod
    def _rs(root, rs_name):
        return root.find(f".//{rs_name}")

    @staticmethod
    def _status(rs):
        if rs is None:
            return "wrong or missing response element (collision?)"
        code = rs.get("statusCode")
        if code not in ("0", "1"):  # 1 = "no matching objects", a valid empty answer
            return f"QBSTATUS {code}: {rs.get('statusMessage')}"
        return None

    def _report_checker(self, kind, frm, to):
        rs_name = "GeneralDetailReportQueryRs" if kind == "gl" else "GeneralSummaryReportQueryRs"

        def chk(root):
            rs = self._rs(root, rs_name)
            err = self._status(rs)
            if err:
                return err
            rep = rs.find(".//ReportRet")
            if rep is None:
                return "no ReportRet"
            title = (rep.findtext("ReportTitle") or "").lower()
            if qbxml.REPORT_TITLE[kind] not in title:
                return f"wrong report title {title!r} (collision?)"
            if kind == "gl":
                dcol = next((cd.get("colID") for cd in rep.findall("ColDesc")
                             for ct in cd.findall("ColTitle")
                             if ct.get("value") == "Date"), None)
                if dcol:
                    for row in rep.iter("DataRow"):
                        for c in row.findall("ColData"):
                            if c.get("colID") == dcol and c.get("value"):
                                d = norm_date(c.get("value"))
                                if d and not (frm <= d <= to):
                                    return f"row dated {d} outside {frm}..{to} (collision?)"
            return None
        return chk

    def _txn_checker(self, rs_name, ret_name, frm, to):
        def chk(root):
            rs = self._rs(root, rs_name)
            err = self._status(rs)
            if err:
                return err
            for ret in rs.findall(ret_name):
                d = ret.findtext("TxnDate")
                if d and not (frm <= d <= to):
                    return f"{ret_name} dated {d} outside {frm}..{to} (collision?)"
            return None
        return chk

    def _plain_checker(self, rs_name):
        return lambda root: self._status(self._rs(root, rs_name))

    # ---------- phases ----------
    def warmup(self):
        """A throwaway CompanyQuery. A cold QuickBooks launch takes a minute or
        two and reliably eats the first request's timeout."""
        log("warm-up ...")
        for attempt in range(4):
            try:
                self._send("warmup", qbxml.SINGLES["company"], 300)
                log("  QuickBooks is answering")
                return
            except TransportError as e:
                log(f"  warm-up attempt {attempt + 1}: {e}")
                time.sleep(60)
        raise CircuitOpen("QuickBooks did not answer a warm-up request")

    def probe(self):
        """QuickBooks' own record count for every list and document type, one
        request per type (a type this edition doesn't support fails alone).
        The benchmark for the completeness check."""
        objects = [("list", t, qbxml.LISTS[t]) for t in qbxml.LISTS]
        objects += [("txn", t, qbxml.TXN_TYPES[t][0]) for t in qbxml.TXN_TYPES]
        for scope, tag, rq in objects:
            key = f"probe_{scope}_{tag}"
            if self.m.ok(key):
                continue
            active = "<ActiveStatus>All</ActiveStatus>" if scope == "list" else ""
            body = f'<{rq} requestID="1" metaData="MetaDataOnly">{active}</{rq}>'
            rs_name = rq.replace("Rq", "Rs")
            root, err = self.pull(key, body, lambda r, n=rs_name: (
                None if r.find(f".//{n}") is not None else "wrong or missing response element"),
                300, attempts=2, meta={"kind": "probe", "object": f"{scope}:{tag}"})
            if root is None:
                # recorded, not fatal: validation decides what an unknown count means
                self.m.put_chunk(key, status="failed", kind="probe",
                                 object=f"{scope}:{tag}", error=err)
                log(f"{key}: no count ({err})")
                continue
            rs = root.find(f".//{rs_name}")
            log(f"{key}: status {rs.get('statusCode')} count {rs.get('retCount')}")

    def probe_status(self, tag):
        """None if QuickBooks counted this document type; otherwise why not."""
        c = self.m.chunk(f"probe_txn_{tag}")
        if not c:
            return None
        if c.get("status") == "failed":
            return c.get("error") or "count request failed"
        root = ET.parse(self.cfg.raw_dir / c["file"]).getroot()
        rs = root.find(f".//{qbxml.TXN_TYPES[tag][0].replace('Rq', 'Rs')}")
        if rs is not None and rs.get("statusCode") not in ("0", "1"):
            return f"QBSTATUS {rs.get('statusCode')}: {rs.get('statusMessage')}"
        return None

    def singles(self):
        for tag, body in qbxml.SINGLES.items():
            key = f"single_{tag}"
            if self.m.ok(key):
                continue
            rs_name = body.split()[0][1:].replace("Rq", "Rs").rstrip("/>")
            root, err = self.pull(key, body, self._plain_checker(rs_name), 300,
                                  meta={"kind": "single", "tag": tag})
            self._result(key, root is not None, err)

    def lists(self):
        for tag in qbxml.LISTS:
            key = f"list_{tag}"
            if self.m.ok(key):
                continue
            rs_name = qbxml.LISTS[tag].replace("Rq", "Rs")
            root, err = self.pull(key, qbxml.list_query(tag), self._plain_checker(rs_name),
                                  self.cfg.list_timeout, meta={"kind": "list", "tag": tag})
            self._result(key, root is not None, err)
            if root is not None:
                n = sum(1 for r in self._rs(root, rs_name) if r.find("ListID") is not None)
                log(f"{key}: {n:,} records")

    def reports(self):
        plan = self.cfg.report_plan()
        todo = [r for r in plan if not self.m.ok(r[0])]
        log(f"reports: {len(plan) - len(todo)} done, {len(todo)} to pull")
        for key, kind, basis, period, frm, to in todo:
            body = (qbxml.gl_report(frm, to, basis) if kind == "gl"
                    else qbxml.summary_report(kind, frm, to, basis))
            # Ledger months aren't split (each month's beginning balances anchor
            # the tie); a month that keeps timing out needs a longer report_timeout.
            root, err = self.pull(key, body, self._report_checker(kind, frm, to),
                                  self.cfg.report_timeout, attempts=2,
                                  meta={"kind": kind, "basis": basis, "period": period,
                                        "from": frm, "to": to})
            self._result(key, root is not None, err)
            if root is not None:
                log(f"{key}: ok")
                if kind == "gl":
                    self._check_collapsed_years(key, root)

    def _check_collapsed_years(self, key, root):
        """A ledger request spanning fiscal years shows P&L detail only for the
        last one; earlier years arrive as nonzero 'Closing Entry' lines. Say so
        immediately: the fix is an earlier books.start, not a re-pull."""
        rep = root.find(".//ReportRet")
        cols = {ct.get("value"): cd.get("colID") for cd in rep.findall("ColDesc")
                for ct in cd.findall("ColTitle") if ct.get("value")}
        tcol, dcol, ccol = cols.get("Type"), cols.get("Debit"), cols.get("Credit")
        years = set()
        for row in rep.iter("DataRow"):
            v = {c.get("colID"): c.get("value") for c in row.findall("ColData")}
            if (v.get(tcol) or "").startswith("Closing Entry") and any(
                    (v.get(x) or "0").replace(",", "") not in ("0", "0.00", "")
                    for x in (dcol, ccol)):
                d = norm_date(v.get(cols.get("Date")) or "")
                years.add(d[:4] if d else "?")
        if years:
            log(f"WARNING {key}: P&L detail for {sorted(years)} is collapsed into closing "
                "entries. Set books.start to the first fiscal year with activity and rerun; "
                "validation will fail until then.")

    def txn_tags(self):
        tags = self.cfg.txn_types or list(qbxml.TXN_TYPES)
        unknown = set(tags) - set(qbxml.TXN_TYPES)
        if unknown:
            raise SystemExit(f"unknown txn types in config: {sorted(unknown)}")
        return [t for t in tags if t not in self.cfg.exclude_txn_types]

    def initial_windows(self, tag):
        step = self.cfg.window_for(tag)
        start, end = self.cfg.books_start, self.cfg.books_end
        out = [(EARLIEST, start - dt.timedelta(days=1))]
        cur = start
        while cur <= end:
            m = cur.month - 1 + step
            nxt = dt.date(cur.year + m // 12, m % 12 + 1, 1)
            out.append((cur, min(nxt - dt.timedelta(days=1), end)))
            cur = nxt
        out.append((end + dt.timedelta(days=1), LATEST))
        return [(a.isoformat(), b.isoformat()) for a, b in out]

    def txns(self):
        for tag in self.txn_tags():
            state = self.m.type_state(tag)
            if state.get("status") == "complete":
                continue
            why = self.probe_status(tag)
            if why:
                state.update(status="unsupported", reason=why)
                self.m.save()
                log(f"{tag}: skipped, QuickBooks didn't count it ({why}). If this edition "
                    "doesn't have the type, add it to extract.exclude_txn_types.")
                continue
            self._pull_type(tag, state)

    def _done_windows(self, tag):
        pat = re.compile(rf"^txn_{re.escape(tag)}_(\d{{8}})_(\d{{8}})$")
        out = []
        for k, c in self.m.data["chunks"].items():
            mm = pat.match(k)
            if mm and c.get("status") == "ok":
                a, b = mm.groups()
                out.append((f"{a[:4]}-{a[4:6]}-{a[6:]}", f"{b[:4]}-{b[4:6]}-{b[6:]}"))
        return out

    @staticmethod
    def _split(frm, to):
        d0, d1 = dt.date.fromisoformat(frm), dt.date.fromisoformat(to)
        mid = d0 + dt.timedelta(days=(d1 - d0).days // 2)
        return [(frm, mid.isoformat()), ((mid + dt.timedelta(days=1)).isoformat(), to)]

    def _pull_type(self, tag, state):
        rq, ret_name, _posting = qbxml.TXN_TYPES[tag]
        rs_name = rq.replace("Rq", "Rs")
        linked = state.get("linked_txns", tag in qbxml.LINKED_TXNS) and tag in qbxml.LINKED_TXNS
        timeout = (self.cfg.txn_timeout_crossref if tag in qbxml.CROSSREF_TYPES
                   else self.cfg.txn_timeout)
        done = self._done_windows(tag)
        windows, gaps = [], []
        queue = self.initial_windows(tag)
        while queue:
            frm, to = queue.pop(0)
            key = f"txn_{tag}_{frm.replace('-', '')}_{to.replace('-', '')}"
            if self.m.ok(key):
                windows.append(key)
                continue
            # A window split on an earlier run: follow the same split now
            # instead of timing out on the whole range again.
            if frm != to and any(frm <= a and b <= to and (a, b) != (frm, to) for a, b in done):
                queue[:0] = self._split(frm, to)
                continue
            meta = {"kind": "txn", "tag": tag, "from": frm, "to": to, "linked_txns": linked}
            chk = self._txn_checker(rs_name, ret_name, frm, to)
            root, err = self.pull(key, qbxml.txn_query(tag, frm, to, True, linked), chk,
                                  timeout, attempts=2, meta=meta, retry_timeouts=False)
            if root is None and linked and err and (
                    "parsing the provided XML" in err or err.startswith("QBSTATUS")):
                # Maybe this query doesn't take IncludeLinkedTxns. Keep the
                # change only if the same window then succeeds.
                meta["linked_txns"] = False
                root, err2 = self.pull(key, qbxml.txn_query(tag, frm, to, True, False), chk,
                                       timeout, attempts=1, meta=meta)
                if root is not None:
                    log(f"{tag}: QuickBooks rejected IncludeLinkedTxns ({err}); continuing "
                        "without it (recorded)")
                    linked = False
                    state["linked_txns"] = False
                    self.m.save()
                else:
                    err = err2
            if root is None and err and "parsing the provided XML" in err:
                self._result(key, False, err)
                raise CircuitOpen(f"{tag}: QuickBooks rejected the request schema ({err}). "
                                  "Shrinking the window can't fix this; fix the request.")
            if root is None and _is_timeout(err) and frm != to:
                queue[:0] = self._split(frm, to)
                log(f"{tag}: {frm}..{to} timed out; splitting")
                continue
            if root is None and _is_timeout(err):
                # a single day: one long last try before recording a hard gap
                root, err = self.pull(key, qbxml.txn_query(tag, frm, to, True, linked), chk,
                                      max(900, timeout), attempts=1, meta=meta)
            if root is None:
                gaps.append({"from": frm, "to": to, "error": err})
                log(f"{tag}: HARD GAP {frm}..{to}: {err} (recorded; validation will fail)")
                self._result(key, False, err)
                continue
            self._result(key, True)
            n = len(self._rs(root, rs_name).findall(ret_name))
            log(f"{key}: {n:,} documents")
            windows.append(key)
        state.update(status="complete" if not gaps else "gaps", windows=windows, gaps=gaps,
                     linked_txns=linked, finished_at=time.strftime("%Y-%m-%dT%H:%M:%S"))
        self.m.save()
        log(f"{tag}: {len(windows)} windows" + (f", {len(gaps)} HARD GAPS" if gaps else ""))

    def run(self, phases=("probe", "singles", "lists", "reports", "txns")):
        self.warmup()
        for ph in phases:
            getattr(self, ph)()
        missing = self.missing()
        if missing:
            log(f"NOT COMPLETE: {len(missing)} items outstanding, first: {missing[:5]}")
            return 1
        (self.cfg.archive_dir / "EXTRACT_COMPLETE").write_text(
            time.strftime("%Y-%m-%dT%H:%M:%S"))
        log("extraction complete")
        return 0

    def missing(self):
        out = [k for k, *_ in self.cfg.report_plan() if not self.m.ok(k)]
        out += [f"list_{t}" for t in qbxml.LISTS if not self.m.ok(f"list_{t}")]
        out += [f"single_{t}" for t in qbxml.SINGLES if not self.m.ok(f"single_{t}")]
        for t in self.txn_tags():
            if self.m.type_state(t).get("status") != "complete":
                out.append(f"type:{t}")
        return out


def main(cfg, transport, phases):
    with Extractor(cfg, transport) as ex:
        try:
            return ex.run(phases)
        except CircuitOpen as e:
            log(f"STOPPED: {e}")
            return 2
        except KeyboardInterrupt:
            log("interrupted; completed chunks are kept")
            return 130


if __name__ == "__main__":
    sys.exit("run via: python -m qbd_archive extract")
