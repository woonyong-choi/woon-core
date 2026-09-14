"""Remove only the known no-op Orca hook when its executable is absent."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from woon_core.settings import apply_json_update


def repair_missing_orca_hooks(codex_home: Path, *, apply: bool = False) -> dict[str, Any]:
    """Preserve all unrelated commands and store a reversible local settings receipt."""
    path = codex_home / "hooks.json"
    executable = codex_home.parent / ".orca/agent-hooks/codex-hook.sh"
    if executable.exists() or not path.is_file():
        return {"status": "ok", "removed": 0, "changed": False}
    before = path.read_bytes()
    current = json.loads(before)
    expected_command = (
        f"if [ -f '{executable}' ] && [ -r '{executable}' ] && [ -x '{executable}' ]; "
        f"then /bin/sh '{executable}'; "
        "else { command -p cat 2>/dev/null || cat; } >/dev/null 2>&1 || :; fi"
    )
    desired = {**current, "hooks": {}}
    removed = 0
    for event, groups in current.get("hooks", {}).items():
        kept = []
        for group in groups:
            hooks = group.get("hooks", [])
            remaining = [hook for hook in hooks if hook.get("command") != expected_command]
            removed += len(hooks) - len(remaining)
            if remaining:
                kept.append({**group, "hooks": remaining})
        if kept:
            desired["hooks"][event] = kept
    result = (
        apply_json_update(
            path,
            desired,
            expected_sha256=hashlib.sha256(before).hexdigest(),
            receipt_root=codex_home / "settings-receipts",
        )
        if apply and removed
        else {"status": "ok", "changed": bool(removed)}
    )
    return {**result, "removed": removed, "applied": apply}
