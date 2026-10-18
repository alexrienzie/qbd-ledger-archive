# Remote bridge

Use this when the extractor should run on a different machine from QuickBooks: a server that stays up overnight, for example, or a Linux box that holds the archive. It works around the SDK's one hard requirement in our experience, that QuickBooks will only start from a logged-on, interactive Windows session. An SSH session doesn't qualify, so the SSH side hands each request to a scheduled task that runs inside the desktop session.

```
extractor --scp--> QB machine: io\req_*.xml
          --ssh--> qbxml-request.ps1 --scheduled task--> qbxml-worker.ps1 --> qbxml-com.ps1 (desktop session) --> QuickBooks
          <--scp-- io\response_<id>.xml   (then deleted from the QB machine)
```

## Setup on the QuickBooks machine

1. Install the OpenSSH server (a Windows optional feature) and set up key-based login for the account you'll connect as. In `sshd_config`, set `PasswordAuthentication no` and limit logins with `AllowUsers`.
2. Copy this folder to `C:\qbd-bridge\` (or set `BRIDGE_DIR` on the caller to wherever you put it; forward slashes, no spaces).
3. Logged in to the desktop as the user QuickBooks runs under, register the task:

   ```powershell
   cd C:\qbd-bridge
   powershell -ExecutionPolicy Bypass -File .\register-task.ps1 -CompanyFile C:\qb-archive\company.qbw
   ```

   This also restricts the folder to that user, SYSTEM and Administrators, because it'll hold company data and its scripts run as that user. The scripts aren't signed; `-ExecutionPolicy Bypass` applies only to the process it starts.

4. Authorize the application once in QuickBooks, as described in [running-an-extraction.md](../../docs/running-an-extraction.md). The first request through the bridge brings up the dialog on the QuickBooks screen.
5. Leave the user logged in. The task runs only while that session exists (disconnecting a remote desktop session is fine; signing out isn't).

## Setup on the caller

The caller needs `bash`, `ssh` and `scp` (on Windows, Git Bash or WSL). In `archive.toml`:

```toml
[transport]
kind = "command"
command = ["bash", "/full/path/to/qbd-ledger-archive/bridge/windows/remote.sh", "qb-host", "{request}", "{response}", "{timeout}"]
grace_seconds = 120
```

`qb-host` is the SSH host name (an alias from `~/.ssh/config` works). `grace_seconds` is how much longer than a request's own timeout the caller waits for the whole round trip.

## Notes

- **One request at a time:** the bridge has a single worker. `qbxml-request.ps1` takes a lock that names its owner and expiry, each request has its own files, and the extractor checks every response's request ID, but don't run two extractors against one bridge.
- **Timeouts:** each request's timeout travels with it to the worker, which runs the QuickBooks call in its own process and ends that process (never QuickBooks) when time runs out. A modal dialog on the QuickBooks screen blocks every request until someone dismisses it.
- **Leftover files:** responses are deleted from the QuickBooks machine once fetched, and requests once answered. `io\worker.log` records one line per request (no company data beyond sizes).

## Status

These scripts repackage the bridge used in the original build (same architecture: a scheduled task in the interactive session, a timed child process, file handoff, response checks) with per-request files, an owned lock and folder permissions. They have been checked for syntax but haven't been run against QuickBooks in this form.
