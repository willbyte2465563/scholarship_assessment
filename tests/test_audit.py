import json

from app.audit import AuditLog, verify


def _log(tmp_path, n=3):
    log = AuditLog(tmp_path / "audit.jsonl")
    for i in range(n):
        log.append("event", {"i": i})
    return log


def test_entries_chain_and_verify(tmp_path):
    log = _log(tmp_path)
    entries = [json.loads(line) for line in log.path.read_text(encoding="utf-8").splitlines()]
    assert [e["seq"] for e in entries] == [1, 2, 3]
    assert entries[1]["prev_hash"] == entries[0]["hash"]
    assert verify(log.path) is None


def test_appending_keeps_earlier_lines_byte_for_byte(tmp_path):
    log = _log(tmp_path, 2)
    before = log.path.read_bytes()
    log.append("event", {"i": 99})
    assert log.path.read_bytes().startswith(before)


def test_edited_entry_is_detected(tmp_path):
    log = _log(tmp_path)
    lines = log.path.read_text(encoding="utf-8").splitlines()
    entry = json.loads(lines[1])
    entry["i"] = 42
    lines[1] = json.dumps(entry)
    log.path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert verify(log.path) == "line 2: contents don't match its hash"


def test_removed_entry_is_detected(tmp_path):
    log = _log(tmp_path)
    lines = log.path.read_text(encoding="utf-8").splitlines()
    log.path.write_text("\n".join([lines[0], lines[2]]) + "\n", encoding="utf-8")
    assert verify(log.path) == "line 2: expected seq 2, found 3"


def test_rehashed_edit_still_breaks_the_next_link(tmp_path):
    from app.redaction import fingerprint

    log = _log(tmp_path)
    lines = log.path.read_text(encoding="utf-8").splitlines()
    entry = json.loads(lines[1])
    entry["i"] = 42
    entry["hash"] = fingerprint({k: v for k, v in entry.items() if k != "hash"})
    lines[1] = json.dumps(entry)
    log.path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert verify(log.path) == "line 3: prev_hash doesn't match the entry before it"
