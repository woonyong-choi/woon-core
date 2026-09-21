"""Bounded local JSON settings adapters with backup, optimistic writes and reread."""

from __future__ import annotations

import hashlib
import json
import math
import subprocess
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from woon_core.errors import WoonError
from woon_core.io import atomic_write, encode_json, exclusive_file_lock
from woon_core.knowledge.graph_colors import graph_color_projection
from woon_core.settings import apply_json_update

# Only documented Workspace/View/Editor/Vault APIs. View state is an opaque public
# value: a Graph color observation is available only when that value exposes it.
_RUNTIME_SNAPSHOT = r"""(async () => {
  const cfg = CONFIG;
  let windowEpoch = null;
  const blocked = reason => JSON.stringify({status: 'blocked', reason, window_epoch: windowEpoch});
  const hash = async value => Array.from(new Uint8Array(await crypto.subtle.digest(
    'SHA-256', new TextEncoder().encode(value)
  )), byte => byte.toString(16).padStart(2, '0')).join('');
  const stable = value => JSON.stringify(value, (_, item) => {
    if (item && !Array.isArray(item) && typeof item === 'object')
      return Object.fromEntries(Object.keys(item).sort().map(key => [key, item[key]]));
    return item;
  });
  if (app.vault.getName() !== cfg.name ||
      typeof app.vault.adapter.getBasePath !== 'function' ||
      app.vault.adapter.getBasePath() !== cfg.path) return blocked('vault-identity-mismatch');
  if (!app.workspace.layoutReady || document.readyState !== 'complete')
    return blocked('workspace-not-ready');
  windowEpoch = performance.timeOrigin;
  if (!Number.isFinite(windowEpoch) || windowEpoch <= 0)
    return blocked('window-context-unavailable');
  const leaves = [];
  app.workspace.iterateAllLeaves(leaf => leaves.push(leaf));
  const graphColors = [];
  const editors = [];
  // Observed navigation panels have no document buffers. Unknown/Canvas editors
  // need their actual editable-state contract before they can be preserved.
  const passive = new Set(['graph', 'localgraph', 'empty', 'file-explorer', 'search',
    'backlink', 'outgoing-link', 'tag', 'outline', 'bookmarks', 'image', 'pdf',
    'recent-files', 'all-properties', 'file-properties', 'linked-graph-view', 'manta-view']);
  const stripColors = value => {
    if (!value || typeof value !== 'object') return;
    delete value.colorGroups;
    for (const child of Object.values(value)) stripColors(child);
  };
  const normalizeView = original => {
    const value = JSON.parse(JSON.stringify(original));
    delete value.icon;
    delete value.title;
    if (value.type === 'graph' || value.type === 'localgraph') stripColors(value.state);
    if (value.type === 'markdown' && value.state) delete value.state.scroll;
    return value;
  };
  const normalizeLayout = value => {
    if (!value || typeof value !== 'object') return;
    if (['leaf', 'split', 'tabs'].includes(value.type)) {
      delete value.width;
      delete value.height;
      delete value.dimension;
    }
    if (value.type === 'leaf' && value.state) value.state = normalizeView(value.state);
    for (const child of Object.values(value)) normalizeLayout(child);
  };
  const getLayout = () => {
    const value = JSON.parse(JSON.stringify(app.workspace.getLayout()));
    delete value['left-ribbon'];
    normalizeLayout(value);
    return value;
  };
  const layout = getLayout();
  const originalLayout = stable(layout);
  const findColors = (value, found) => {
    if (!value || typeof value !== 'object') return;
    if (Object.hasOwn(value, 'colorGroups')) found.push(value.colorGroups);
    for (const child of Object.values(value)) findColors(child, found);
  };
  for (const leaf of leaves) {
    const view = leaf.view;
    const type = view.getViewType();
    if (type === 'graph' || type === 'localgraph') {
      const found = [];
      findColors(leaf.getViewState().state, found);
      if (found.length === 1 && Array.isArray(found[0]))
        graphColors.push(await hash(stable(found[0])));
    } else if (type === 'markdown') {
      if (leaf.isDeferred === true) {
        const state = leaf.getViewState();
        const file = state.state?.file;
        // Only an unloaded reading view has no editor buffer or selection to lose.
        // Deferred source editors stay blocked until their real buffer is observable.
        if (state.type !== 'markdown' || state.state?.mode !== 'preview' ||
            typeof file !== 'string' || !file.endsWith('.md') || file.startsWith('/') ||
            file.split('/').includes('..') || view.editor ||
            typeof view.getViewData === 'function')
          return blocked('deferred-markdown-state-unverifiable');
        const savedState = stable(state);
        const disk = await app.vault.adapter.read(file);
        if (leaf.isDeferred !== true || stable(leaf.getViewState()) !== savedState ||
            await app.vault.adapter.read(file) !== disk)
          return blocked('deferred-markdown-changed-during-inspection');
        editors.push({file, content: await hash(disk), mode: 'preview', selections: []});
        continue;
      }
      if (!view.file || typeof view.getViewData !== 'function')
        return blocked('markdown-buffer-unavailable');
      const mode = view.getMode();
      if (mode === 'source' && (!view.editor || typeof view.editor.getValue !== 'function'))
        return blocked('markdown-editor-unavailable');
      const text = view.getViewData();
      const disk = await app.vault.adapter.read(view.file.path);
      if (text !== disk || view.getViewData() !== text ||
          (mode === 'source' && view.editor.getValue() !== text))
        return blocked('unsaved-or-changing-markdown-buffer');
      editors.push({file: view.file.path, content: await hash(text),
        mode, selections: mode === 'source' ? view.editor.listSelections() : []});
    } else if (!passive.has(type) || view.editor || typeof view.getViewData === 'function')
      return blocked('unsupported-view-unsaved-state');
  }
  const active = app.workspace.activeLeaf;
  const activeState = active ? normalizeView(active.getViewState()) : null;
  const session = await hash(stable({layout, editors, activeState,
    activeFile: app.workspace.getActiveFile()?.path ?? null}));
  const graphDisk = await hash(await app.vault.adapter.read('.obsidian/graph.json'));
  if (stable(getLayout()) !== originalLayout)
    return blocked('workspace-changed-during-inspection');
  if (cfg.save) {
    if (session !== cfg.session || graphDisk !== cfg.graphDisk)
      return blocked('workspace-or-settings-changed-before-layout-save');
    app.workspace.requestSaveLayout();
    await app.workspace.requestSaveLayout.run();
  }
  return JSON.stringify({status: 'ok', session_sha256: session,
    graph_disk_sha256: graphDisk, graph_colors_sha256: graphColors, window_epoch: windowEpoch});
})()"""


class _RuntimeSnapshotError(WoonError):
    """Keep an observed window identity even when its buffers cannot be verified."""

    def __init__(self, reason: str, window_epoch: object = None):
        super().__init__(f"runtime reload blocked: {reason}")
        self.window_epoch = (
            window_epoch
            if isinstance(window_epoch, (int, float))
            and not isinstance(window_epoch, bool)
            and math.isfinite(window_epoch)
            and window_epoch > 0
            else None
        )


@contextmanager
def _runtime_attempt(root: Path, *, enabled: bool) -> Iterator[Callable[..., None]]:
    """Record the latest attempt separately from the last verified reload receipt."""
    attempt: dict[str, Any] = {
        "runtime_contract_version": 3,
        "status": "in-progress",
        "reload_requested": False,
        "reload_command_returned": False,
        "new_window_observed": False,
        "reload_verified": False,
        "ui_verified": False,
        "stages": [],
    }

    def mark(stage: str, **values: Any) -> None:
        attempt.update(stage=stage, **values)
        attempt["stages"].append({"stage": stage, "time": time.time()})
        if enabled:
            atomic_write(root / "navigation-runtime-attempt.json", encode_json(attempt), mode=0o600)

    mark("preflight")
    try:
        yield mark
    except (WoonError, OSError) as error:
        epoch = getattr(error, "window_epoch", None)
        values: dict[str, Any] = {"status": "failed", "error": str(error)}
        if epoch is not None and attempt["reload_requested"]:
            values.update(
                after_window_epoch=epoch,
                new_window_observed=epoch != attempt.get("before_window_epoch"),
            )
        mark(attempt["stage"], **values)
        if enabled:
            raise WoonError(
                f"runtime reload failed at {attempt['stage']} "
                f"(reload_requested={attempt['reload_requested']}, "
                f"new_window_observed={attempt['new_window_observed']}): {error}"
            ) from error
        raise


def _runtime_cli(cli: Path, name: str, command: str, *arguments: str) -> str:
    try:
        result = subprocess.run(
            [str(cli), f"vault={name}", command, *arguments],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        # CLI output/arguments can contain private data; never echo them.
        raise WoonError(f"Obsidian {command} unavailable; no runtime verification") from error
    if result.returncode:
        raise WoonError(f"Obsidian {command} failed; no runtime verification")
    return result.stdout.strip()


def _runtime_snapshot(
    cli: Path, name: str, vault: Path, *, save: dict[str, Any] | None = None
) -> dict[str, Any]:
    config = {"name": name, "path": str(vault), "save": save is not None}
    if save:
        config.update(session=save["session_sha256"], graphDisk=save["graph_disk_sha256"])
    code = _RUNTIME_SNAPSHOT.replace("CONFIG", json.dumps(config), 1)
    try:
        # Supported CLI eval prefixes its returned value with '=> '.
        raw = _runtime_cli(cli, name, "eval", f"code={code}").removeprefix("=> ")
        result = json.loads(raw)
        if isinstance(result, str):
            result = json.loads(result)
    except ValueError as error:
        raise WoonError("Obsidian eval did not return a completed JSON snapshot") from error
    if not isinstance(result, dict) or result.get("status") != "ok":
        reason = result.get("reason") if isinstance(result, dict) else "invalid-snapshot"
        epoch = result.get("window_epoch") if isinstance(result, dict) else None
        raise _RuntimeSnapshotError(str(reason), epoch)
    for key in ("session_sha256", "graph_disk_sha256"):
        digest = result.get(key)
        if not isinstance(digest, str) or len(digest) != 64:
            raise WoonError("invalid runtime snapshot digest")
    epoch = result.get("window_epoch")
    if (
        not isinstance(epoch, (int, float))
        or isinstance(epoch, bool)
        or not math.isfinite(epoch)
        or epoch <= 0
    ):
        raise WoonError("invalid runtime window epoch")
    colors = result.get("graph_colors_sha256")
    if not isinstance(colors, list) or any(
        not isinstance(value, str) or len(value) != 64 for value in colors
    ):
        raise WoonError("invalid runtime Graph color observations")
    return result


def reload_navigation_runtime(
    vault: Path,
    *,
    obsidian_cli: Path,
    vault_name: str,
    apply: bool = False,
    expected_state: str | None = None,
) -> dict[str, Any]:
    """Prepare or explicitly reload an observed, saved workspace using public CLI APIs.

    Call only for an already open, operator-controlled Vault. This never saves
    document buffers or repairs user state. Missing evidence blocks before reload;
    failed postchecks leave existing receipts intact and never issue a success receipt.
    Color reflection always remains pending a separate real Graph observation.
    """
    vault = vault.resolve(strict=True)
    if not obsidian_cli.is_absolute() or not obsidian_cli.is_file() or not vault_name.strip():
        raise WoonError("runtime reload requires an explicit CLI path and Vault name")
    if apply and not expected_state:
        raise WoonError("runtime reload --apply requires --expected-state from preparation")
    graph = vault / ".obsidian/graph.json"
    root = vault / ".local/woon-knowledge/settings-receipts"
    if graph.is_symlink() or not graph.is_file() or not root.is_dir() or root.is_symlink():
        raise WoonError("runtime reload requires regular Graph settings and existing receipts")
    before = graph.read_bytes()
    disk_hash = hashlib.sha256(before).hexdigest()
    current = json.loads(before)
    if not isinstance(current, dict) or not isinstance(current.get("colorGroups"), list):
        raise WoonError("Graph color settings unavailable")
    receipts = []
    for path in root.glob("*.json"):
        if path.is_symlink():
            continue
        data = path.read_bytes()
        try:
            record = json.loads(data)
        except ValueError:
            continue
        if (
            isinstance(record, dict)
            and record.get("status") == "ok"
            and record.get("target") == "graph.json"
            and record.get("sha256") == disk_hash
            and record.get("verification") == "disk-reread; UI-not-checked"
        ):
            receipts.append((path, data))
    if not receipts:
        raise WoonError("no successful settings receipt matches current graph.json")
    receipt, receipt_bytes = sorted(receipts)[0]
    with _runtime_attempt(root, enabled=apply) as mark:
        snapshot = _runtime_snapshot(obsidian_cli, vault_name, vault)
        mark(
            "preflight-verified",
            before_window_epoch=snapshot["window_epoch"],
            baseline={
                "vault": str(vault),
                "vault_name": vault_name,
                "session_sha256": snapshot["session_sha256"],
                "graph_disk_sha256": disk_hash,
                "settings_receipt_sha256": hashlib.sha256(receipt_bytes).hexdigest(),
            },
        )
        if snapshot["graph_disk_sha256"] != disk_hash:
            raise WoonError("Graph settings changed or runtime Vault disk differs")
        state = hashlib.sha256(
            encode_json(
                {
                    "vault": str(vault),
                    "snapshot": snapshot,
                    "receipt": hashlib.sha256(receipt_bytes).hexdigest(),
                }
            )
        ).hexdigest()
        report = {
            "status": "ready",
            "runtime_contract_version": 3,
            "applied": False,
            "scope": "navigation-runtime-reload",
            "expected_state": state,
            "settings_sha256": disk_hash,
            "ui_verified": False,
        }
        if not apply:
            return report
        if expected_state != state:
            raise WoonError("workspace or settings changed since preparation; reload not executed")
        with exclusive_file_lock(root / "settings.lock"):
            if graph.read_bytes() != before or receipt.read_bytes() != receipt_bytes:
                raise WoonError("settings or receipt changed before reload")
            mark("layout-save")
            _runtime_snapshot(obsidian_cli, vault_name, vault, save=snapshot)
            latest = _runtime_snapshot(obsidian_cli, vault_name, vault)
            if latest != snapshot or graph.read_bytes() != before:
                raise WoonError("workspace changed after layout save; reload not executed")
            mark("reload-requested", reload_requested=True)
            _runtime_cli(obsidian_cli, vault_name, "reload")
            mark("reconnect", reload_command_returned=True)
            after = _reconnect_snapshot(obsidian_cli, vault_name, vault, snapshot["window_epoch"])
            mark(
                "new-window-observed",
                after_window_epoch=after["window_epoch"],
                new_window_observed=True,
            )
            # A ready old window, or the first frame of a new window, is insufficient:
            # startup plugins can replace a tab shortly after the layout appears.
            mark("settled-postcheck")
            time.sleep(1)
            settled = _runtime_snapshot(obsidian_cli, vault_name, vault)
            desired_hash = hashlib.sha256(
                json.dumps(
                    current["colorGroups"],
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
            if (
                after["session_sha256"] != snapshot["session_sha256"]
                or after["graph_disk_sha256"] != disk_hash
                or settled["session_sha256"] != snapshot["session_sha256"]
                or settled["graph_disk_sha256"] != disk_hash
                or settled["window_epoch"] != after["window_epoch"]
                or graph.read_bytes() != before
                or receipt.read_bytes() != receipt_bytes
            ):
                raise WoonError("reload postcheck failed; no success receipt; inspect workspace")
            colors = settled["graph_colors_sha256"]
            color_state = (
                "public-view-state-match"
                if colors and all(value == desired_hash for value in colors)
                else "public-view-state-mismatch"
                if colors
                else "public-view-state-unavailable"
            )
            report.update(
                status="pending-visual-verification",
                applied=True,
                reload_verified=True,
                before_window_epoch=snapshot["window_epoch"],
                after_window_epoch=settled["window_epoch"],
                observation_interval_ms=1000,
                runtime_colors=color_state,
                session_sha256=after["session_sha256"],
                settings_receipt_sha256=hashlib.sha256(receipt_bytes).hexdigest(),
                verification="public-view-state; saved-buffers; layout; active-view; disk-reread",
            )
            mark("write-runtime-receipt")
            atomic_write(root / "navigation-runtime.json", encode_json(report), mode=0o600)
            mark("complete", status=report["status"], reload_verified=True)
        return report


def _reconnect_snapshot(cli: Path, name: str, vault: Path, before_epoch: float) -> dict[str, Any]:
    """Retry only reads while the new window and CLI transport become ready."""
    for attempt in range(3):
        try:
            snapshot = _runtime_snapshot(cli, name, vault)
            if snapshot["window_epoch"] == before_epoch:
                raise WoonError("reload still returns the previous window context")
            return snapshot
        except WoonError as error:
            reconnecting = str(error) in {
                "runtime reload blocked: workspace-not-ready",
                "Obsidian eval unavailable; no runtime verification",
                "Obsidian eval failed; no runtime verification",
                "Obsidian eval did not return a completed JSON snapshot",
                "reload still returns the previous window context",
            }
            if not reconnecting or attempt == 2:
                raise
            time.sleep(attempt + 1)
    raise AssertionError("unreachable reconnect loop")


def reconnect_navigation_runtime(
    vault: Path, *, obsidian_cli: Path, vault_name: str, expected_attempt: str
) -> dict[str, Any]:
    """Read a failed reload's current window without saving, reloading or writing receipts.

    Old attempts without a pre-reload baseline provide current observations only.
    A changed attempt, settings, window or session fails without altering user state.
    """
    vault = vault.resolve(strict=True)
    if not obsidian_cli.is_absolute() or not obsidian_cli.is_file() or not vault_name.strip():
        raise WoonError("runtime reconnect requires an explicit CLI path and Vault name")
    root = vault / ".local/woon-knowledge/settings-receipts"
    path = root / "navigation-runtime-attempt.json"
    graph = vault / ".obsidian/graph.json"
    if root.is_symlink() or any(item.is_symlink() or not item.is_file() for item in (path, graph)):
        raise WoonError("runtime reconnect requires regular attempt and Graph settings files")
    attempt_bytes = path.read_bytes()
    if hashlib.sha256(attempt_bytes).hexdigest() != expected_attempt:
        raise WoonError("runtime attempt changed; inspect it before reconnecting")
    attempt = json.loads(attempt_bytes)
    if (
        not isinstance(attempt, dict)
        or attempt.get("runtime_contract_version") != 3
        or attempt.get("status") != "failed"
        or attempt.get("reload_requested") is not True
    ):
        raise WoonError("runtime reconnect requires a failed, already-requested reload")
    epoch = attempt.get("before_window_epoch")
    if (
        not isinstance(epoch, (int, float))
        or isinstance(epoch, bool)
        or not math.isfinite(epoch)
        or epoch <= 0
    ):
        raise WoonError("failed runtime attempt has no valid previous window epoch")
    before = graph.read_bytes()
    disk_hash = hashlib.sha256(before).hexdigest()
    baseline = attempt.get("baseline")
    receipt: Path | None = None
    receipt_bytes: bytes | None = None
    if baseline is not None:
        if (
            not isinstance(baseline, dict)
            or baseline.get("vault") != str(vault)
            or baseline.get("vault_name") != vault_name
            or baseline.get("graph_disk_sha256") != disk_hash
            or not isinstance(baseline.get("session_sha256"), str)
            or len(baseline["session_sha256"]) != 64
        ):
            raise WoonError("runtime reconnect baseline or Graph settings do not match")
        for candidate in sorted(root.glob("*.json")):
            if candidate.is_symlink():
                continue
            data = candidate.read_bytes()
            if hashlib.sha256(data).hexdigest() == baseline.get("settings_receipt_sha256"):
                receipt, receipt_bytes = candidate, data
                break
        if receipt is None:
            raise WoonError("runtime reconnect settings receipt no longer matches the baseline")
    after = _reconnect_snapshot(obsidian_cli, vault_name, vault, epoch)
    time.sleep(1)
    settled = _runtime_snapshot(obsidian_cli, vault_name, vault)
    if (
        settled["window_epoch"] != after["window_epoch"]
        or settled["session_sha256"] != after["session_sha256"]
        or any(item["graph_disk_sha256"] != disk_hash for item in (after, settled))
        or graph.read_bytes() != before
        or path.read_bytes() != attempt_bytes
        or (receipt is not None and receipt.read_bytes() != receipt_bytes)
        or (baseline is not None and after["session_sha256"] != baseline["session_sha256"])
    ):
        raise WoonError("runtime reconnect postcheck failed; inspect workspace and attempt")
    return {
        "status": "pending-visual-verification" if baseline else "observed-only",
        "scope": "navigation-runtime-reconnect",
        "runtime_contract_version": 3,
        "reload_commands": 0,
        "layout_saved": False,
        "receipts_written": 0,
        "attempt_sha256": expected_attempt,
        "before_window_epoch": epoch,
        "after_window_epoch": settled["window_epoch"],
        "new_window_observed": True,
        "settings_sha256": disk_hash,
        "session_sha256": settled["session_sha256"],
        "observation_interval_ms": 1000,
        "reload_verified": baseline is not None,
        "pre_reload_preservation": "verified" if baseline else "unverified-missing-baseline",
        "ui_verified": False,
    }


def configure_navigation(
    vault: Path, *, apply: bool = False, graph_colors: bool = False
) -> dict[str, Any]:
    """Apply the declared Graph/search presentation policy without opening Obsidian."""
    if graph_colors:
        projection = graph_color_projection(vault)
        path = vault / ".obsidian/graph.json"
        before = path.read_bytes()
        current = json.loads(before)
        if not isinstance(current, dict):
            raise WoonError("graph.json must contain an object")
        desired = {**current, "colorGroups": projection["colorGroups"]}
        expected = hashlib.sha256(before).hexdigest()
        result = (
            apply_json_update(
                path,
                desired,
                expected_sha256=expected,
                receipt_root=vault / ".local/woon-knowledge/settings-receipts",
            )
            if apply
            else {"changed": desired != current, "before_sha256": expected}
        )
        return {
            "status": "ok",
            "applied": apply,
            "scope": "graph-colors",
            "allowed_keys": ["colorGroups"],
            "settings": {"graph": result},
            "legend": projection["legend"],
            "input_sha256": projection["input_sha256"],
            "ui_verified": False,
        }
    policy = json.loads((vault / "config/obsidian-navigation.json").read_text())
    if policy.get("version") != 1:
        raise WoonError("unsupported Obsidian navigation policy")
    targets = {
        "graph": ".obsidian/graph.json",
        "omnisearch": ".obsidian/plugins/omnisearch/data.json",
    }
    results = {}
    for name, relative in targets.items():
        path = vault / relative
        before = path.read_bytes()
        current = json.loads(before)
        updates = policy.get(name)
        if name == "graph" and isinstance(updates, dict):
            # Producer inputs, never Obsidian search/display settings.
            updates = {
                key: value
                for key, value in updates.items()
                if key not in {"book_source_registry", "category_colors"}
            }
        allowed = {"search"} if name == "graph" else {"hideExcluded", "downrankedFoldersFilters"}
        if not isinstance(updates, dict) or set(updates) != allowed:
            raise WoonError(f"invalid navigation policy fields: {name}")
        desired = {**current, **updates}
        expected = hashlib.sha256(before).hexdigest()
        results[name] = (
            apply_json_update(
                path,
                desired,
                expected_sha256=expected,
                receipt_root=vault / ".local/woon-knowledge/settings-receipts",
            )
            if apply
            else {"changed": desired != current, "before_sha256": expected}
        )
    return {"status": "ok", "applied": apply, "settings": results, "ui_verified": False}
