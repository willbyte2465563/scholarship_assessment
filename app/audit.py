"""Append-only, hash-chained audit log (JSON Lines).

Each entry carries the previous entry's hash and its own, so editing, removing or reordering
any entry breaks the chain from that point on. Entries hold hashes, versions, IDs and model
output (which only ever quotes the redacted view). Never raw applicant data.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path

from app.redaction import fingerprint

GENESIS = "0" * 64


class AuditLog:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()

    def _tail(self) -> tuple[int, str]:
        seq, prev = 0, GENESIS
        if self.path.exists():
            with self.path.open(encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        entry = json.loads(line)
                        seq, prev = entry["seq"], entry["hash"]
        return seq, prev

    def append(self, event: str, data: dict) -> dict:
        # Reading the tail and writing the next entry must not interleave across threads.
        with self._lock:
            seq, prev = self._tail()
            entry = {
                "seq": seq + 1,
                "at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                "event": event,
                "prev_hash": prev,
                **data,
            }
            entry["hash"] = fingerprint(entry)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            return entry


def verify(path: Path) -> str | None:
    """None if the chain is intact, otherwise a description of the first broken entry."""
    prev, expected_seq = GENESIS, 1
    with path.open(encoding="utf-8") as f:
        for n, line in enumerate(f, start=1):
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                return f"line {n}: not valid JSON"
            body = {k: v for k, v in entry.items() if k != "hash"}
            if entry.get("seq") != expected_seq:
                return f"line {n}: expected seq {expected_seq}, found {entry.get('seq')}"
            if entry.get("prev_hash") != prev:
                return f"line {n}: prev_hash doesn't match the entry before it"
            if fingerprint(body) != entry.get("hash"):
                return f"line {n}: contents don't match its hash"
            prev, expected_seq = entry["hash"], expected_seq + 1
    return None
