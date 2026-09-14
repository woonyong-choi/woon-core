"""Read-only workflow diagnosis, independent of successful write checkpoints."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from woon_core.errors import WoonError
from woon_core.io import load_yaml
from woon_core.knowledge.orchestration import (
    load_orchestrator_settings,
    verify_codex_automation_registry,
)
from woon_core.registry import Registry


def active_instruction_files(repository: Path) -> tuple[Path, ...]:
    """Exclude declared raw archives from the executable instruction inventory."""
    ignored_parts = {".git", ".local", ".venv", "node_modules", "archive", "_sources"}
    manifest = repository / ".woon/repository.yaml"
    raw_roots = load_yaml(manifest).get("path_audit_raw_roots", []) if manifest.is_file() else []
    if not isinstance(raw_roots, list) or not all(isinstance(item, str) for item in raw_roots):
        raise WoonError("instruction inventory raw roots must be string paths")
    excluded = tuple(repository / item for item in raw_roots)
    candidates = (*repository.rglob("AGENTS.md"), *repository.rglob("CLAUDE.md"))
    return tuple(
        sorted(
            path
            for path in candidates
            if not path.is_symlink()
            and not ignored_parts.intersection(path.relative_to(repository).parts)
            and not any(path.is_relative_to(raw) for raw in excluded)
        )
    )


def validate_instruction_references(paths: tuple[Path, ...]) -> None:
    for path in paths:
        if path.name not in {"AGENTS.md", "CLAUDE.md"}:
            continue
        content = path.read_text(encoding="utf-8").lower()
        if any(
            marker in content
            for marker in ("ai-reference", "_quarantine", "woon-brain", "codex-write-vault")
        ):
            raise WoonError(f"retired active instruction reference: {path.name}")


def instruction_inventory(vault: Path, automation_root: Path) -> tuple[Path, ...]:
    """Inventory active control files without following private evidence or symlinks."""
    workspace = vault.parent
    registry_path = workspace / "woon-core/registry/repositories.yaml"
    if registry_path.is_file():
        registry = Registry.load(workspace)
        repositories = [workspace / item.directory for item in registry.repositories.values()]
    else:
        repositories = [workspace / name for name in ("woon-core", "woon-skills", "woon-env")]
    paths: set[Path] = set()
    for repository in {*repositories, vault}:
        paths.update(active_instruction_files(repository))
        for name in (
            "AGENTS.md",
            "CLAUDE.md",
            ".github/copilot-instructions.md",
            ".woon/repository.yaml",
        ):
            path = repository / name
            if path.is_file() and not path.is_symlink():
                paths.add(path)
        for name in ("config", "docs", "policies", "standards", "registry", "schema"):
            root = repository / name
            for path in root.rglob("*"):
                if (
                    path.is_file()
                    and not path.is_symlink()
                    and path.suffix in {".md", ".yaml", ".yml", ".json", ".toml"}
                ):
                    paths.add(path)
    for name in ("AGENTS.md", "config.toml", "hooks.json"):
        path = automation_root.parent / name
        if path.is_file():
            paths.add(path)
    paths.update(automation_root.glob("*/automation.toml"))
    paths.update((vault / ".obsidian").glob("*.json"))
    for name in ("manifest.json", "data.json"):
        paths.update((vault / ".obsidian/plugins").glob("*/" + name))
    return tuple(sorted(paths))


def summarize_health(payload: dict[str, Any], *, sample_limit: int = 3) -> dict[str, Any]:
    """Keep actionable counts and bounded examples; never return the full issue dump."""
    issues = payload.get("issues", {})
    return {
        "issue_counts": payload.get("issue_counts", {}),
        "samples": {
            key: values[:sample_limit]
            for key, values in issues.items()
            if isinstance(values, list) and values
        },
        "markdown_files_scanned": payload.get("markdown_files_scanned", 0),
    }


def diagnose_workflow(vault: Path, automation_root: Path) -> dict[str, Any]:
    """Collect all available diagnoses, including when another check fails.

    This function never records a success receipt or advances a cursor. Texts of
    local AI configuration and automation prompts are represented only by hashes.
    """
    report: dict[str, Any] = {"status": "ok", "receipt_recorded": False, "checks": {}}
    checks = report["checks"]
    try:
        paths = instruction_inventory(vault, automation_root)
        validate_instruction_references(paths)
        digest = hashlib.sha256()
        for path in paths:
            digest.update(path.as_posix().encode())
            digest.update(b"\0")
            digest.update(path.read_bytes())
        checks["instruction-inventory"] = {
            "status": "ok",
            "files": len(paths),
            "sha256": digest.hexdigest(),
        }
    except (OSError, WoonError) as error:
        checks["instruction-inventory"] = {"status": "blocked", "reason": str(error)}
    try:
        skills = skill_inventory(vault.parent, installed_root=automation_root.parent / "skills")
        checks["skill-parity"] = {"status": "ok", "files": len(skills)}
    except (OSError, WoonError) as error:
        checks["skill-parity"] = {"status": "blocked", "reason": str(error)}
    try:
        settings = load_orchestrator_settings(vault)
        for lane in settings.enabled_automations:
            try:
                verify_codex_automation_registry(
                    settings, automation_root, lane_id=lane.automation_id
                )
                checks[lane.automation_id] = {"status": "ok", "owner": lane.owner}
            except WoonError as error:
                checks[lane.automation_id] = {
                    "status": "blocked",
                    "owner": lane.owner,
                    "reason": str(error),
                }
    except WoonError as error:
        checks["automation-contract"] = {"status": "blocked", "reason": str(error)}
    script = Path(__file__).parent / "vault_tools/audit-vault-health.py"
    try:
        run = subprocess.run(
            [sys.executable, str(script)],
            cwd=vault,
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
        payload = json.loads(run.stdout)
        checks["vault-health"] = {
            "status": "ok" if run.returncode == 0 else "blocked",
            **summarize_health(payload),
        }
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        checks["vault-health"] = {"status": "blocked", "reason": type(error).__name__}
    if any(check["status"] != "ok" for check in checks.values()):
        report["status"] = "blocked"
    return report


def skill_inventory(workspace: Path, *, installed_root: Path | None = None) -> tuple[Path, ...]:
    """Return canonical skills and reject drift in active installed copies."""

    repository = workspace / "woon-skills"
    if not repository.is_dir():
        return ()
    catalog = repository / "catalog.json"
    if not catalog.is_file():
        raise WoonError("second-brain governance skill catalog is missing")
    canonical_skills = tuple(sorted((repository / "skills").rglob("SKILL.md")))
    if not canonical_skills:
        raise WoonError("second-brain governance canonical skill inventory is empty")
    canonical = tuple(
        sorted(
            path
            for root in (
                repository / "skills",
                repository / "profiles",
                repository / "conflicts",
                repository / "standards",
                repository / "evals",
            )
            for path in root.rglob("*")
            if path.is_file() and path.suffix in {".json", ".md", ".py", ".sh", ".yaml", ".yml"}
        )
    )
    active_root = installed_root or (Path.home() / ".codex/skills")
    installed: list[Path] = []
    for source in canonical_skills:
        active = active_root / source.parent.name / "SKILL.md"
        if not active.exists():
            continue
        if not active.is_file() or active.read_bytes() != source.read_bytes():
            raise WoonError(f"second-brain governance installed skill drift: {source.parent.name}")
        installed.append(active)
    return (catalog, *canonical, *sorted(installed))
