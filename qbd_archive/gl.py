"""Parser for the QuickBooks General Ledger detail report.

The report is a flat run of rows that encodes the account tree by position:

  DataRow  "Sales"                 header (no Type, no Date); may carry a balance
  DataRow  "Retail"                child header; its Balance is the beginning balance
  DataRow  Invoice 3/1/2019 ...    posting row
  SubtotalRow "Total Retail"       closes the child section
  SubtotalRow "Total Sales"        closes the parent

Rules learned from real files:

* A full account name is rebuilt from the open sections ("Sales:Retail"),
  matching how the trial balance names accounts.
* A header followed directly by another header is a parent whose balance is
  the aggregate of its children; that balance is not a beginning balance.
* Postings made directly to a parent that has children appear under a
  "<Parent> - Other" subsection, and belong to the parent itself.
* Some sections (retained earnings is the common one) close with a DataRow
  "Total X" instead of a SubtotalRow.
* Columns are found by title from ColDesc, never by position.
* In some files, stretches of the report omit section headers entirely, so
  posting rows arrive with no open section, or under the parent when the
  child header is missing. Every posting row also carries the Account
  column, so each row is placed by the rules in resolve(), and the rule
  used is recorded on the row. Nothing is guessed: a row that no rule
  places uniquely stays unresolved and fails validation.
"""
import bisect
import xml.etree.ElementTree as ET

from .money import cents, norm_date


class AccountIndex:
    """Short-name and full-name lookup over the chart of accounts."""

    def __init__(self, accounts):
        # accounts: iterable of (name, fullname)
        self.by_short = {}
        self.fullnames = set()
        for name, fullname in accounts:
            if not fullname:
                continue
            self.fullnames.add(fullname)
            self.by_short.setdefault(name or fullname.split(":")[-1], []).append(fullname)


def _parent_short(fullname):
    parts = fullname.split(":")
    return parts[-2] if len(parts) >= 2 else None


def _strip_other(label):
    return label[:-8] if label.endswith(" - Other") else label


def parse(xml_bytes_or_path, accounts):
    """Yield one dict per posting row and one per beginning balance.

    Rows: {"row": n, "kind": "line"|"begin", ...}. `n` counts DataRows from 1
    in document order and is stored as src_row for traceability.
    """
    root = (ET.parse(xml_bytes_or_path).getroot() if not isinstance(xml_bytes_or_path, bytes)
            else ET.fromstring(xml_bytes_or_path))
    rep = root.find(".//ReportRet")
    if rep is None:
        raise ValueError("no ReportRet in GL response")
    colmap = {}
    for cd in rep.findall("ColDesc"):
        for ct in cd.findall("ColTitle"):
            if ct.get("value"):
                colmap[ct.get("value")] = cd.get("colID")
    rd = rep.find("ReportData")
    nodes = list(rd) if rd is not None else []
    vals = [{c.get("colID"): c.get("value") for c in n.findall("ColData")} for n in nodes]
    type_col, date_col, acct_col = colmap.get("Type"), colmap.get("Date"), colmap.get("Account")

    def is_header(i):
        return (nodes[i].tag == "DataRow" and vals[i].get(type_col) is None
                and not norm_date(vals[i].get(date_col) or ""))

    # subtotal labels in document order, for the fallback rules
    sub_idx, sub_lbl = [], []
    for i, n in enumerate(nodes):
        if n.tag == "SubtotalRow":
            sub_idx.append(i)
            sub_lbl.append(vals[i].get("1") or "")

    def g(v, title):
        return v.get(colmap.get(title, "?"))

    stack, row = [], 0
    for i, node in enumerate(nodes):
        v = vals[i]
        if node.tag == "SubtotalRow":
            label = v.get("1") or ""
            if label.startswith("Total ") and stack and stack[-1] == label[6:]:
                stack.pop()
            continue
        if node.tag != "DataRow":
            continue
        row += 1
        if is_header(i):
            label = v.get("1") or ""
            if label.startswith("Total ") and stack and stack[-1] == label[6:]:
                stack.pop()  # a section closed by a DataRow (retained earnings)
                continue
            stack.append(label)
            nxt = i + 1
            if (nxt < len(nodes) and is_header(nxt)
                    and (vals[nxt].get("1") or "") != "Total " + label):
                continue  # aggregate parent: its balance is the children's sum
            bal = cents(g(v, "Balance"))
            if bal is not None:
                yield {"kind": "begin", "row": row, "account": _fullname(stack),
                       "balance_c": bal}
            continue
        section = _fullname(stack)
        short = (v.get(acct_col) or "").strip() if acct_col else ""
        account, how = resolve(section, short, i, accounts, sub_idx, sub_lbl)
        yield {"kind": "line", "row": row, "txn_type": g(v, "Type"),
               "txn_date": norm_date(g(v, "Date")), "ref_number": g(v, "Num"),
               "txn_number": g(v, "Trans #"), "name": g(v, "Name"), "memo": g(v, "Memo"),
               "account": account, "account_src": how, "account_col": short or None,
               "debit_c": cents(g(v, "Debit")), "credit_c": cents(g(v, "Credit")),
               "running_balance_c": cents(g(v, "Balance"))}


def _fullname(stack):
    if stack and stack[-1].endswith(" - Other"):
        return ":".join(stack[:-1])
    return ":".join(stack)


def resolve(section, short, node_i, accounts, sub_idx, sub_lbl):
    """Place one posting row. Returns (fullname or None, rule)."""
    if section:
        leaf = section.split(":")[-1]
        if not short or short == leaf or short == section:
            return section, "section"
        if f"{section}:{short}" in accounts.fullnames:
            # child header missing while its parent section is open
            return f"{section}:{short}", "section_child"
        # The Account column names some other account: the section structure
        # can't be trusted for this row, so leave it for a person to place.
        return None, "section_conflict"
    if not short:
        return None, "unresolved"
    if short in accounts.fullnames and ":" in short:
        return short, "account_column"
    cands = accounts.by_short.get(short, [])
    if len(cands) == 1:
        return cands[0], "account_column"
    if not cands:
        return None, "unresolved"
    # Several accounts share this short name under different parents. The
    # subtotal rows that follow name the enclosing parents.
    start = bisect.bisect_right(sub_idx, node_i)
    forward = {_strip_other(l[6:]) for l in sub_lbl[start:] if l.startswith("Total ")}
    hits = [f for f in cands if _parent_short(f) in forward]
    if len(hits) == 1:
        return hits[0], "subtotal_cascade"
    # Nearest enclosing parent in document order: after this row's own
    # "Total <short>", the first "Total <parent>" that names a candidate.
    parents = {}
    for f in cands:
        if _parent_short(f):
            parents.setdefault(_parent_short(f), []).append(f)
    j = next((k for k in range(start, len(sub_idx)) if sub_lbl[k] == f"Total {short}"), None)
    if j is not None:
        for k in range(j + 1, len(sub_idx)):
            lbl = sub_lbl[k]
            if lbl.startswith("Total ") and _strip_other(lbl[6:]) in parents:
                hit = parents[_strip_other(lbl[6:])]
                # two candidates under same-named parents can't be told apart
                return (hit[0], "subtotal_proximity") if len(hit) == 1 else (None, "unresolved")
    return None, "unresolved"
