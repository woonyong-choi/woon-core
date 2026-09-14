"""Bounded local JSON settings adapters with backup, optimistic writes and reread."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from woon_core.errors import WoonError
from woon_core.io import atomic_write, encode_json, exclusive_file_lock


def apply_json_update(
    path: Path, value: dict[str, Any], *, expected_sha256: str, receipt_root: Path
) -> dict[str, Any]:
    """Apply a caller-scoped setting, preserving old bytes and checking disk again."""
    if path.is_symlink() or not path.is_file():
        raise WoonError("settings target must be an existing regular file")
    receipt_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    receipt_root.chmod(0o700)
    with exclusive_file_lock(receipt_root / "settings.lock"):
        before = path.read_bytes()
        if hashlib.sha256(before).hexdigest() != expected_sha256:
            raise WoonError("settings changed after inspection; replan before applying")
        if json.loads(before) == value:
            return {"status": "ok", "changed": False, "sha256": expected_sha256}
        after = encode_json(value)
        after_hash = hashlib.sha256(after).hexdigest()
        backup = receipt_root / "backups" / f"{expected_sha256}.json"
        if backup.exists() and backup.read_bytes() != before:
            raise WoonError("settings backup collision")
        backup.parent.mkdir(exist_ok=True, mode=0o700)
        backup.parent.chmod(0o700)
        atomic_write(backup, before, mode=0o600)
        atomic_write(path, after, mode=path.stat().st_mode & 0o777)
        if path.read_bytes() != after:
            raise WoonError("settings changed during reread; backup preserved, no success receipt")
        result = {
            "status": "ok",
            "changed": True,
            "before_sha256": expected_sha256,
            "sha256": after_hash,
            "backup": backup.name,
            "target": path.name,
            "verification": "disk-reread; UI-not-checked",
        }
        receipt_id = hashlib.sha256((str(path) + expected_sha256 + after_hash).encode()).hexdigest()
        atomic_write(receipt_root / f"{receipt_id}.json", encode_json(result), mode=0o600)
        return result
