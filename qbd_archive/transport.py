"""How qbXML gets to QuickBooks and back.

QuickBooks Desktop exposes exactly one programmatic interface: the SDK's
request processor (COM object QBXMLRP2.RequestProcessor), which takes a
qbXML string and returns a qbXML string. Two constraints shape everything:

1. The COM call must run inside a logged-on, interactive Windows session on
   the machine where QuickBooks is installed. A service or an SSH session
   cannot start QuickBooks.
2. QuickBooks has one UI thread. A modal dialog on its screen blocks every
   SDK call until someone dismisses it.

Transports:

  com      Run on the QuickBooks machine itself, in the logged-on session.
           Each request runs in a child process with a hard timeout, so a
           wedged call is abandoned instead of hanging the extractor.
           Requires pywin32.
  command  Hand each request to an external command (for example an SSH
           call into a scheduled-task bridge, see bridge/windows/). The
           command gets the request file path and must write the response.

Every transport raises TransportError with QuickBooks' own message text;
the extractor's retry policy keys off that text.
"""
import os
import signal
import subprocess
import sys
import tempfile
from pathlib import Path


class TransportError(Exception):
    pass


def _run(args, timeout):
    """subprocess.run with a timeout that also kills grandchildren (an ssh
    started by a wrapper script would otherwise hold the pipes open)."""
    kw = {"stdout": subprocess.PIPE, "stderr": subprocess.PIPE, "text": True}
    if os.name == "nt":
        kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    p = subprocess.Popen(args, **kw)
    try:
        out, err = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)],
                           capture_output=True)
        else:
            os.killpg(p.pid, signal.SIGKILL)
        p.communicate()
        raise TransportError(f"timeout after {timeout}s")
    return subprocess.CompletedProcess(args, p.returncode, out, err)


class ComTransport:
    def __init__(self, company_file, app_name="qbd-ledger-archive", app_id=""):
        if os.name != "nt":
            raise SystemExit("transport 'com' runs only on Windows, on the QuickBooks "
                             "machine; use transport 'command' from elsewhere")
        self.company_file = str(company_file)
        self.app_name = app_name
        self.app_id = app_id

    def send(self, request_xml, timeout):
        # The temp folder holds one request and response (company data) and is
        # removed when the call returns.
        with tempfile.TemporaryDirectory(prefix="qbdarch_") as td:
            req = Path(td) / "request.xml"
            resp = Path(td) / "response.xml"
            req.write_text(request_xml, encoding="utf-8")
            args = [sys.executable, "-m", "qbd_archive.com_worker",
                    "--company-file", self.company_file, "--app-name", self.app_name,
                    "--app-id", self.app_id, "--request", str(req), "--response", str(resp)]
            r = _run(args, timeout)
            if r.returncode != 0 or not resp.exists():
                raise TransportError((r.stderr or r.stdout or "worker failed").strip()[-400:])
            return resp.read_text(encoding="utf-8")


class CommandTransport:
    """argv template with {request}, {response} and {timeout} placeholders."""

    def __init__(self, argv, grace=120):
        if not argv:
            raise SystemExit("transport 'command' needs transport.command = [argv...]")
        self.argv = list(argv)
        self.grace = grace

    def send(self, request_xml, timeout):
        with tempfile.TemporaryDirectory(prefix="qbdarch_") as td:
            req = Path(td) / "request.xml"
            resp = Path(td) / "response.xml"
            req.write_text(request_xml, encoding="utf-8")
            args = [a.replace("{request}", str(req)).replace("{response}", str(resp))
                     .replace("{timeout}", str(timeout)) for a in self.argv]
            r = _run(args, timeout + self.grace)
            if r.returncode != 0 or not resp.exists() or resp.stat().st_size == 0:
                tail = (r.stdout or "") + (r.stderr or "")
                raise TransportError(tail.strip()[-400:] or f"command exit {r.returncode}")
            return resp.read_text(encoding="utf-8")


def from_config(cfg):
    t = cfg.transport or {}
    kind = t.get("kind", "com")
    if kind == "com":
        if not t.get("company_file"):
            raise SystemExit("transport.company_file is required for transport 'com'")
        return ComTransport(t["company_file"], t.get("app_name", "qbd-ledger-archive"),
                            t.get("app_id", ""))
    if kind == "command":
        return CommandTransport(t.get("command"), int(t.get("grace_seconds", 120)))
    raise SystemExit(f"unknown transport kind {kind!r}")
