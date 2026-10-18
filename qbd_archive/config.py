"""Archive configuration (TOML) and the period plan derived from it.

The period plan is what makes the archive tie: every report chunk belongs to
exactly one tie-out period, and every period has its own trial balance.

  pre     everything before books_start, in one catch-all chunk
  FYxxxx  one period per fiscal year, GL pulled month by month
  post    everything after books_end, in one catch-all chunk (normally empty)

The catch-alls exist so that nothing outside the expected range can be
silently left behind.
"""
import datetime as dt
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

EARLIEST = dt.date(1900, 1, 1)
LATEST = dt.date(2099, 12, 31)

# Window size in months per source-document type. The cost driver inside
# QuickBooks is cross-reference resolution, not row count: a payment that
# applies to many invoices is expensive to materialize. Expensive types start
# small; the extractor bisects any window that still times out.
DEFAULT_WINDOW_MONTHS = {
    "invoice": 1, "salesorder": 1, "recvpayment": 1, "billpayck": 1,
    "billpaycc": 1, "deposit": 1, "buildassembly": 1,
    "cccharge": 3, "creditmemo": 3, "arrefundcc": 3,
}


@dataclass
class Period:
    label: str            # "pre", "FY2019", "post"
    start: dt.date
    end: dt.date
    gl_chunks: list       # [(label, start, end)] monthly GL chunks in the period


@dataclass
class Config:
    path: Path
    archive_dir: Path
    company_label: str
    books_start: dt.date
    books_end: dt.date
    fiscal_year_start_month: int = 1
    bases: list = field(default_factory=lambda: ["accrual"])
    qbxml_version: str = "16.0"
    transport: dict = field(default_factory=dict)
    txn_types: list = None            # None = every type in qbxml.TXN_TYPES
    exclude_txn_types: list = field(default_factory=list)
    window_months: dict = field(default_factory=dict)
    report_timeout: int = 900
    txn_timeout: int = 420
    txn_timeout_crossref: int = 180
    list_timeout: int = 420
    max_consecutive_failures: int = 8

    # ---------- derived paths ----------
    @property
    def raw_dir(self):
        return self.archive_dir / "raw"

    @property
    def request_dir(self):
        return self.archive_dir / "requests"

    @property
    def manifest_path(self):
        return self.archive_dir / "manifest.json"

    @property
    def db_path(self):
        return self.archive_dir / f"{self.company_label}.sqlite"

    @property
    def validation_dir(self):
        return self.archive_dir / "validation"

    def window_for(self, tag):
        return self.window_months.get(tag, DEFAULT_WINDOW_MONTHS.get(tag, 12))

    # ---------- period plan ----------
    def fiscal_years(self):
        """[(label, start, end)] fiscal years from books_start through books_end;
        the last one is truncated at books_end."""
        out = []
        start = self.books_start
        while start <= self.books_end:
            nxt = add_months(start, 12)
            end = min(nxt - dt.timedelta(days=1), self.books_end)
            # label by the calendar year the fiscal year ends in
            label = f"FY{(nxt - dt.timedelta(days=1)).year}"
            out.append((label, start, end))
            start = nxt
        return out

    def report_plan(self):
        """Every report chunk the archive needs: [(key, kind, basis, period, from, to)].

        Keys carry their date range, so changing books.start or books.end
        produces new keys and the old chunks are never reused. Extract pulls
        exactly these, load loads exactly these, and validation fails if any
        is missing."""
        out = []
        for basis in self.bases:
            for p in self.periods():
                for label, frm, to in p.gl_chunks:
                    out.append((f"gl_{basis}_{label}_{frm:%Y%m%d}_{to:%Y%m%d}", "gl", basis,
                                p.label, frm.isoformat(), to.isoformat()))
                for kind in ("tb", "pnl", "bs"):
                    out.append((f"{kind}_{basis}_{p.label}_{p.start:%Y%m%d}_{p.end:%Y%m%d}",
                                kind, basis, p.label, p.start.isoformat(), p.end.isoformat()))
        return out

    def periods(self):
        pre_end = self.books_start - dt.timedelta(days=1)
        out = [Period("pre", EARLIEST, pre_end, [("pre", EARLIEST, pre_end)])]
        for label, start, end in self.fiscal_years():
            out.append(Period(label, start, end, month_windows(start, end)))
        post_start = self.books_end + dt.timedelta(days=1)
        out.append(Period("post", post_start, LATEST, [("post", post_start, LATEST)]))
        return out


def add_months(d, n):
    m = d.month - 1 + n
    return dt.date(d.year + m // 12, m % 12 + 1, 1)


def month_windows(start, end):
    """[(YYYY-MM, start, end)] calendar months covering start..end."""
    out = []
    cur = dt.date(start.year, start.month, 1)
    while cur <= end:
        nxt = add_months(cur, 1)
        out.append((cur.strftime("%Y-%m"), max(cur, start),
                    min(nxt - dt.timedelta(days=1), end)))
        cur = nxt
    return out


def _date(v, name):
    try:
        return dt.date.fromisoformat(str(v))
    except ValueError as e:
        raise SystemExit(f"config: {name} must be YYYY-MM-DD, got {v!r}") from e


def load(path):
    path = Path(path).resolve()
    if not path.exists():
        raise SystemExit(f"config file not found: {path} (copy archive.example.toml to start)")
    try:
        with open(path, "rb") as f:
            raw = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise SystemExit(f"config file {path} is not valid TOML: {e}. Windows paths need "
                         "single quotes, e.g. company_file = 'C:\qb\company.qbw'") from e
    a = raw.get("archive", {})
    b = raw.get("books", {})
    x = raw.get("extract", {})
    archive_dir = Path(a.get("dir", "archive"))
    if not archive_dir.is_absolute():
        archive_dir = (path.parent / archive_dir).resolve()
    cfg = Config(
        path=path,
        archive_dir=archive_dir,
        company_label=a.get("company_label", "company"),
        books_start=_date(b.get("start"), "books.start"),
        books_end=_date(b.get("end"), "books.end"),
        fiscal_year_start_month=int(b.get("fiscal_year_start_month", 1)),
        bases=list(b.get("bases", ["accrual"])),
        qbxml_version=str(raw.get("qbxml", {}).get("version", "16.0")),
        transport=raw.get("transport", {"kind": "com"}),
        txn_types=x.get("txn_types"),
        exclude_txn_types=list(x.get("exclude_txn_types", [])),
        window_months=dict(x.get("window_months", {})),
        report_timeout=int(x.get("report_timeout", 900)),
        txn_timeout=int(x.get("txn_timeout", 420)),
        txn_timeout_crossref=int(x.get("txn_timeout_crossref", 180)),
        list_timeout=int(x.get("list_timeout", 420)),
        max_consecutive_failures=int(x.get("max_consecutive_failures", 8)),
    )
    if not 1 <= cfg.fiscal_year_start_month <= 12:
        raise SystemExit("config: books.fiscal_year_start_month must be 1-12")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", cfg.company_label):
        raise SystemExit("config: archive.company_label may use letters, digits, '.', '_' and '-' only")
    bad_win = {k: v for k, v in cfg.window_months.items() if not (isinstance(v, int) and v >= 1)}
    if bad_win:
        raise SystemExit(f"config: extract.window_months values must be whole months >= 1: {bad_win}")
    if cfg.books_start.day != 1 or cfg.books_start.month != cfg.fiscal_year_start_month:
        raise SystemExit("config: books.start must be the first day of a fiscal year "
                         f"(fiscal_year_start_month = {cfg.fiscal_year_start_month})")
    if cfg.books_end < cfg.books_start:
        raise SystemExit("config: books.end is before books.start")
    if "accrual" not in cfg.bases:
        raise SystemExit("config: books.bases must include 'accrual' "
                         "(the books-of-record basis the archive ties on)")
    bad = set(cfg.bases) - {"accrual", "cash"}
    if bad:
        raise SystemExit(f"config: unknown basis {sorted(bad)}")
    return cfg
