"""Command line: qbd-archive <command> --config archive.toml  (or: python -m qbd_archive ...)

  extract    pull everything from QuickBooks into raw/ (resumable)
  load       rebuild the SQLite archive from raw/
  validate   run the acceptance gates; exit 0 only if every gate passes
  status     what the manifest says is done and outstanding
  run        extract, load, validate
"""
import argparse
import sys

from . import config as config_mod


def main(argv=None):
    ap = argparse.ArgumentParser(prog="qbd-archive", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["extract", "load", "validate", "status", "run"])
    ap.add_argument("--config", "-c", default="archive.toml")
    ap.add_argument("--phases", default="probe,singles,lists,reports,txns",
                    help="extract phases, comma separated")
    a = ap.parse_args(argv)
    cfg = config_mod.load(a.config)
    cfg.archive_dir.mkdir(parents=True, exist_ok=True)
    phases = [p.strip() for p in a.phases.split(",") if p.strip()]

    if a.command in ("extract", "run"):
        from . import extract, transport
        rc = extract.main(cfg, transport.from_config(cfg), phases)
        if rc and a.command == "run":
            print("extraction isn't complete; run `status` to see what's outstanding, or "
                  "`load` and `validate` to inspect what's there so far")
        if rc or a.command == "extract":
            return rc
    if a.command in ("load", "run"):
        from . import load
        rc = load.main(cfg)
        if rc or a.command == "load":
            return rc
    if a.command in ("validate", "run"):
        from . import validate
        return validate.main(cfg)
    if a.command == "status":
        return status(cfg)
    return 0


def status(cfg):
    from .extract import Extractor
    from .manifest import Manifest
    from .qbxml import LINKED_TXNS

    m = Manifest(cfg.manifest_path)
    chunks = m.data["chunks"]
    by_kind = {}
    for c in chunks.values():
        by_kind[c.get("kind")] = by_kind.get(c.get("kind"), 0) + 1
    print(f"archive: {cfg.archive_dir}")
    print("raw responses by kind: " + ", ".join(f"{k} {v}" for k, v in sorted(by_kind.items(), key=str)))
    for tag, st in sorted(m.data["types"].items()):
        gaps = st.get("gaps") or []
        print(f"  {tag:14s} {st.get('status', '?'):9s} {len(st.get('windows', [])):5d} windows"
              + (f"  {len(gaps)} HARD GAPS" if gaps else "")
              + ("  (no linked txns)" if tag in LINKED_TXNS and not st.get("linked_txns", True)
                 else "")
              + (f"  ({st['reason']})" if st.get("reason") else ""))
    ex = Extractor.__new__(Extractor)
    ex.cfg, ex.m = cfg, m
    missing = ex.missing()
    print(f"outstanding: {len(missing)}" + (f" (first: {missing[:6]})" if missing else ""))
    return 0 if not missing else 1


if __name__ == "__main__":
    sys.exit(main())
