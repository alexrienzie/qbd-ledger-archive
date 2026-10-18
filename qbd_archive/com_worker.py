"""One qbXML request through the QuickBooks SDK, in its own process.

Run by ComTransport; not meant to be called by hand. Opening a fresh
connection and session per request costs a little time but means a
timed-out request can be abandoned by killing this process, without
touching QuickBooks itself.

Never kill qbw.exe to recover from a stall. Repeated force-quits trip
QuickBooks' integrated-application tamper check (the app's unattended
access grant is revoked and has to be re-approved in the GUI) and can leave
ghost sessions on a multi-user host that lock the file for everyone.
"""
import argparse
import sys

QB_FILE_OPEN_DO_NOT_CARE = 2
LOCAL_QBD = 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--company-file", required=True)
    ap.add_argument("--app-name", required=True)
    ap.add_argument("--app-id", default="")
    ap.add_argument("--request", required=True)
    ap.add_argument("--response", required=True)
    a = ap.parse_args()
    try:
        import win32com.client  # pywin32
    except ImportError:
        sys.exit("pywin32 is not installed (pip install pywin32)")
    with open(a.request, encoding="utf-8") as f:
        request = f.read()
    rp = win32com.client.Dispatch("QBXMLRP2.RequestProcessor")
    ticket = None
    try:
        rp.OpenConnection2(a.app_id, a.app_name, LOCAL_QBD)
        # Always pass the file path: it attaches to an instance that already
        # has this file open, or launches QuickBooks with it.
        ticket = rp.BeginSession(a.company_file, QB_FILE_OPEN_DO_NOT_CARE)
        response = rp.ProcessRequest(ticket, request)
    except Exception as e:  # COM errors carry QuickBooks' message text
        sys.exit(f"ERR: {e}")
    finally:
        try:
            if ticket is not None:
                rp.EndSession(ticket)
            rp.CloseConnection()
        except Exception:
            pass
    with open(a.response, "w", encoding="utf-8") as f:
        f.write(response)


if __name__ == "__main__":
    main()
