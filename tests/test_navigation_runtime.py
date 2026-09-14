"""Reload boundaries: command targeting, concurrent edits, and non-success receipts."""

import hashlib
import json
import shutil
import subprocess
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import pytest

from woon_core.cli import run
from woon_core.errors import WoonError
from woon_core.knowledge import local_settings
from woon_core.settings import apply_json_update


def _prepared_vault(tmp_path: Path) -> tuple[Path, Path, Path]:
    vault = tmp_path / "vault"
    graph = vault / ".obsidian/graph.json"
    graph.parent.mkdir(parents=True)
    graph.write_text('{"search":"private query","scale":0.123,"custom":true}')
    root = vault / ".local/woon-knowledge/settings-receipts"
    apply_json_update(
        graph,
        {**json.loads(graph.read_bytes()), "colorGroups": [{"query": "topic", "color": 123}]},
        expected_sha256=hashlib.sha256(graph.read_bytes()).hexdigest(),
        receipt_root=root,
    )
    cli = tmp_path / "obsidian-cli"
    cli.touch()
    return vault, cli, root


@pytest.mark.parametrize(
    "failure",
    [
        None,
        "concurrent-session",
        "post-reload-session",
        "reconnecting",
        "old-window",
        "late-home",
        "post-deferred-source",
        "incomplete-json",
        "persistent-incomplete-json",
    ],
)
def test_reload_targets_exact_vault_and_never_promotes_missing_visual_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str | None
) -> None:
    vault, cli, root = _prepared_vault(tmp_path)
    if failure in {"post-deferred-source", "persistent-incomplete-json"}:
        (root / "navigation-runtime.json").write_text('{"status":"previous-verification"}')
    graph = vault / ".obsidian/graph.json"
    before = graph.read_bytes()
    receipts = {path.name: path.read_bytes() for path in root.glob("*.json")}
    commands = []
    changed = False
    reloaded = False
    post_reads = 0

    def execute(arguments: list[str], **kwargs: object) -> SimpleNamespace:
        nonlocal reloaded, post_reads
        assert arguments[:2] == [str(cli), "vault=Expected Vault"]
        assert kwargs["timeout"] == 15
        commands.append(arguments[2])
        if arguments[2] == "reload":
            reloaded = True
            return SimpleNamespace(returncode=0, stdout="Reloading...")
        assert arguments[2] == "eval"
        if reloaded:
            post_reads += 1
            if failure == "persistent-incomplete-json" or (
                failure == "incomplete-json" and post_reads == 1
            ):
                return SimpleNamespace(returncode=0, stdout="=> Promise { <pending> }")
            if failure == "post-deferred-source":
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps(
                        {
                            "status": "blocked",
                            "reason": "deferred-markdown-state-unverifiable",
                            "window_epoch": 2,
                        }
                    ),
                )
            if failure == "reconnecting" and post_reads == 1:
                return SimpleNamespace(
                    returncode=0,
                    stdout='=> {"status":"blocked","reason":"workspace-not-ready"}',
                )
        return SimpleNamespace(
            returncode=0,
            stdout="=> "
            + json.dumps(
                {
                    "status": "ok",
                    "session_sha256": "b" * 64
                    if changed
                    or (reloaded and failure == "post-reload-session")
                    or (failure == "late-home" and post_reads > 1)
                    else "a" * 64,
                    "graph_disk_sha256": hashlib.sha256(before).hexdigest(),
                    "graph_colors_sha256": [],  # Actual native Graph public state is empty.
                    "window_epoch": 2 if reloaded and failure != "old-window" else 1,
                }
            ),
        )

    monkeypatch.setattr(local_settings.subprocess, "run", execute)
    monkeypatch.setattr(local_settings.time, "sleep", lambda seconds: None)
    arguments = [
        "knowledge",
        "configure-navigation",
        "--vault",
        str(vault),
        "--runtime-reload",
        "--obsidian-cli",
        str(cli),
        "--vault-name",
        "Expected Vault",
    ]
    output = StringIO()
    run(arguments, output)
    prepared = json.loads(output.getvalue())
    assert prepared["status"] == "ready"
    assert commands == ["eval"]
    assert {path.name: path.read_bytes() for path in root.glob("*.json")} == receipts
    changed = failure == "concurrent-session"
    applying = [*arguments, "--apply", "--expected-state", prepared["expected_state"]]
    if failure in {
        "concurrent-session",
        "post-reload-session",
        "old-window",
        "late-home",
        "post-deferred-source",
        "persistent-incomplete-json",
    }:
        with pytest.raises(
            WoonError,
            match=(
                "changed since preparation|postcheck failed|previous window context|deferred|JSON"
            ),
        ):
            run(applying, StringIO())
        assert reloaded == (failure != "concurrent-session")
        assert commands.count("reload") <= 1
        if failure not in {"post-deferred-source", "persistent-incomplete-json"}:
            assert not (root / "navigation-runtime.json").exists()
        attempt = json.loads((root / "navigation-runtime-attempt.json").read_bytes())
        assert attempt["status"] == "failed"
        assert attempt["reload_requested"] == reloaded
        assert attempt["reload_verified"] is False
        assert attempt["baseline"]["graph_disk_sha256"] == hashlib.sha256(before).hexdigest()
        if failure == "persistent-incomplete-json":
            assert post_reads == 3
        if failure == "post-deferred-source":
            assert attempt["stage"] == "reconnect"
            assert attempt["new_window_observed"] is True
            assert attempt["before_window_epoch"] == 1 and attempt["after_window_epoch"] == 2
    else:
        output = StringIO()
        run(applying, output)
        report = json.loads(output.getvalue())
        assert commands.count("reload") == 1
        assert report["status"] == "pending-visual-verification"
        assert report["runtime_colors"] == "public-view-state-unavailable"
        assert report["reload_verified"] is True
        assert report["runtime_contract_version"] == 3
        assert report["before_window_epoch"] != report["after_window_epoch"]
        assert report["ui_verified"] is False
        assert json.loads((root / "navigation-runtime.json").read_bytes()) == report
    assert graph.read_bytes() == before  # Includes unrelated zoom/search/user settings.
    for name, content in receipts.items():
        assert (root / name).read_bytes() == content


@pytest.mark.parametrize(
    "case", ["legacy", "baseline", "changed-session", "stale-attempt", "concurrent-attempt"]
)
def test_reconnect_only_reads_without_repeating_reload_or_rewriting_receipts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    vault, cli, root = _prepared_vault(tmp_path)
    graph = vault / ".obsidian/graph.json"
    graph_bytes = graph.read_bytes()
    settings_receipt = next(root.glob("*.json"))
    attempt: dict[str, object] = {
        "runtime_contract_version": 3,
        "status": "failed",
        "reload_requested": True,
        "before_window_epoch": 1,
    }
    if case != "legacy":
        attempt["baseline"] = {
            "vault": str(vault),
            "vault_name": "Expected Vault",
            "session_sha256": "a" * 64,
            "graph_disk_sha256": hashlib.sha256(graph_bytes).hexdigest(),
            "settings_receipt_sha256": hashlib.sha256(settings_receipt.read_bytes()).hexdigest(),
        }
    path = root / "navigation-runtime-attempt.json"
    path.write_text(json.dumps(attempt))
    expected = hashlib.sha256(path.read_bytes()).hexdigest()
    (root / "navigation-runtime.json").write_text('{"status":"previous-verification"}')
    originals = {item.name: item.read_bytes() for item in root.glob("*.json")}
    calls = []

    def execute(arguments: list[str], **_kwargs: object) -> SimpleNamespace:
        calls.append(arguments)
        assert arguments[:3] == [str(cli), "vault=Expected Vault", "eval"]
        assert '"save": false' in arguments[3]
        if len(calls) == 1:
            return SimpleNamespace(returncode=0, stdout="=> Promise { <pending> }")
        if case == "concurrent-attempt" and len(calls) == 3:
            path.write_bytes(path.read_bytes() + b"\n")
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "status": "ok",
                    "session_sha256": ("b" if case == "changed-session" else "a") * 64,
                    "graph_disk_sha256": hashlib.sha256(graph_bytes).hexdigest(),
                    "graph_colors_sha256": [],
                    "window_epoch": 2,
                }
            ),
        )

    monkeypatch.setattr(local_settings.subprocess, "run", execute)
    monkeypatch.setattr(local_settings.time, "sleep", lambda _seconds: None)
    arguments = [
        "knowledge",
        "configure-navigation",
        "--vault",
        str(vault),
        "--runtime-reconnect",
        "--obsidian-cli",
        str(cli),
        "--vault-name",
        "Expected Vault",
        "--expected-attempt",
        "0" * 64 if case == "stale-attempt" else expected,
    ]
    output = StringIO()
    if case in {"changed-session", "stale-attempt", "concurrent-attempt"}:
        with pytest.raises(WoonError, match="attempt changed|postcheck failed"):
            run(arguments, output)
    else:
        run(arguments, output)
        report = json.loads(output.getvalue())
        assert report["reload_commands"] == report["receipts_written"] == 0
        assert report["layout_saved"] is report["ui_verified"] is False
        assert report["new_window_observed"] is True
        assert report["reload_verified"] == (case == "baseline")
        assert report["pre_reload_preservation"] == (
            "verified" if case == "baseline" else "unverified-missing-baseline"
        )
    assert len(calls) == (0 if case == "stale-attempt" else 3)
    assert graph.read_bytes() == graph_bytes
    for name, before in originals.items():
        suffix = b"\n" if case == "concurrent-attempt" and name == path.name else b""
        assert (root / name).read_bytes() == before + suffix


@pytest.mark.parametrize("extra", ["--apply", "--runtime-reload"])
def test_reconnect_rejects_mutation_options_before_calling_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, extra: str
) -> None:
    monkeypatch.setattr(
        local_settings.subprocess, "run", lambda *_args, **_kwargs: pytest.fail("must not call CLI")
    )
    with pytest.raises(WoonError, match="reconnect only reads"):
        run(
            [
                "knowledge",
                "configure-navigation",
                "--vault",
                str(tmp_path),
                "--runtime-reconnect",
                "--obsidian-cli",
                "/explicit/cli",
                "--vault-name",
                "Vault",
                "--expected-attempt",
                "a" * 64,
                extra,
            ],
            StringIO(),
        )


@pytest.mark.parametrize(
    "reason", ["vault-identity-mismatch", "unsaved-or-changing-markdown-buffer"]
)
def test_runtime_guard_failure_cannot_reload_or_create_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reason: str
) -> None:
    vault, cli, root = _prepared_vault(tmp_path)
    commands = []

    def execute(arguments: list[str], **kwargs: object) -> SimpleNamespace:
        commands.append(arguments[2])
        return SimpleNamespace(
            returncode=0, stdout=json.dumps({"status": "blocked", "reason": reason})
        )

    monkeypatch.setattr(local_settings.subprocess, "run", execute)
    with pytest.raises(WoonError, match=reason):
        local_settings.reload_navigation_runtime(
            vault,
            obsidian_cli=cli,
            vault_name="Expected Vault",
            apply=True,
            expected_state="a" * 64,
        )
    assert commands == ["eval"]
    assert not (root / "navigation-runtime.json").exists()


def test_public_deferred_preview_preserves_session_but_never_hides_an_editor() -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required to exercise the actual public-API snapshot script")
    setup = r"""
globalThis.crypto = require('node:crypto').webcrypto;
globalThis.document = {readyState: 'complete'};
let mode = 'preview', deferred = false, unsaved = false, missing = false;
const leaf = {
  get isDeferred() { return deferred; },
  getViewState: () => ({type: 'markdown', state: {file: 'note.md', mode}}),
  get view() {
    if (deferred || missing) return {getViewType: () => 'markdown'};
    return {getViewType: () => 'markdown', file: {path: 'note.md'}, getMode: () => mode,
      getViewData: () => unsaved ? 'unsaved text' : 'saved text',
      editor: mode === 'source' ? {
        getValue: () => unsaved ? 'unsaved text' : 'saved text', listSelections: () => []
      } : undefined};
  }
};
globalThis.app = {
  vault: {getName: () => 'Test', adapter: {
    getBasePath: () => '/vault',
    read: async file => file === '.obsidian/graph.json' ? '{}' : 'saved text'
  }},
  workspace: {layoutReady: true, activeLeaf: null, getActiveFile: () => null,
    iterateAllLeaves: callback => callback(leaf),
    getLayout: () => ({main: {type: 'leaf', id: 'same-leaf', state: leaf.getViewState()}})
  }
};
"""
    snapshot = local_settings._RUNTIME_SNAPSHOT.replace(
        "CONFIG", json.dumps({"name": "Test", "path": "/vault", "save": False}), 1
    )
    script = (
        setup
        + "\n(async () => { const read = async () => JSON.parse(await eval("
        + (json.dumps(snapshot))
        + r"""));
      const loaded = await read();
      deferred = true; const reading = await read();
      mode = 'source'; const deferredSource = await read();
      deferred = false; unsaved = true; const edited = await read();
      mode = 'preview'; unsaved = false; missing = true; const unavailable = await read();
      process.stdout.write(JSON.stringify({loaded, reading, deferredSource, edited, unavailable}));
    })();"""
    )
    completed = subprocess.run([node, "-e", script], capture_output=True, text=True, check=True)
    result = json.loads(completed.stdout)
    assert result["loaded"]["status"] == result["reading"]["status"] == "ok"
    assert result["loaded"]["session_sha256"] == result["reading"]["session_sha256"]
    assert result["deferredSource"]["reason"] == "deferred-markdown-state-unverifiable"
    assert result["edited"]["reason"] == "unsaved-or-changing-markdown-buffer"
    assert result["unavailable"]["reason"] == "markdown-buffer-unavailable"
