"""The extraction manifest: one entry per raw response, plus per-type status.

The manifest is the checkpoint (a killed run resumes where it stopped) and
the provenance record (every loaded row points back to a manifest key, whose
entry holds the request hash, response hash, size and pull time).
"""
import json
import os
import time
from pathlib import Path


class Manifest:
    def __init__(self, path):
        self.path = Path(path)
        self.data = json.loads(self.path.read_text()) if self.path.exists() else {
            "format": 1, "chunks": {}, "types": {}}

    def chunk(self, key):
        return self.data["chunks"].get(key)

    def ok(self, key):
        c = self.chunk(key)
        return bool(c and c.get("status") == "ok")

    def put_chunk(self, key, **entry):
        entry.setdefault("pulled_at", time.strftime("%Y-%m-%dT%H:%M:%S"))
        self.data["chunks"][key] = entry
        self.save()

    def type_state(self, tag):
        return self.data["types"].setdefault(tag, {})

    def save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1, sort_keys=True))
        os.replace(tmp, self.path)

    def chunks_of(self, kind):
        return {k: v for k, v in self.data["chunks"].items() if v.get("kind") == kind}
