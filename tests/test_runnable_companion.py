from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from woon_core.errors import WoonError
from woon_core.knowledge import runnable_companion as module
from woon_core.knowledge.obsidian_plugins import ObsidianPluginService


@pytest.fixture
def companion(request, tmp_path, monkeypatch):
    plugin_id = getattr(request, "param", "runnable-code-blocks")
    vault = tmp_path / "vault"
    plugin = vault / ".obsidian/plugins" / plugin_id
    plugin.mkdir(parents=True)
    legacy = {"remoteExecutionEnabled": False, "kotlinPath": "/legacy", "custom": {"keep": 42}}
    for name, body in {
        "manifest.json": json.dumps({"id": plugin_id, "version": "0.7.2"}),
        "main.js": "plugin",
        "styles.css": "style",
        "data.json": json.dumps(
            {"locale": "ko", "run": legacy} if plugin_id == "manta" else legacy
        ),
    }.items():
        (plugin / name).write_text(body)
    artifact = tmp_path / "approved.mjs"
    artifact.write_bytes(b"approved companion fixture")
    node, docker, cli, runtime_node = [
        tmp_path / name for name in ("node", "docker", "obsidian", "real-node")
    ]
    for path in (node, docker, cli, runtime_node):
        path.touch()
    policy = {
        "version": 1,
        "release": "0.7.2",
        "host": "127.0.0.1",
        "port": 17171,
        "protocol_version": 1,
        "artifact_sha256": module._sha(artifact.read_bytes()),
        "required_language": "kotlin",
        "required_image": "docker.io/example/kotlin@sha256:" + "a" * 64,
    }
    service = ObsidianPluginService(vault)
    manager = module.RunnableCompanionManager(service, home=tmp_path / "home", policy=policy)
    state = SimpleNamespace(
        running=False,
        spawns=[],
        commands=[],
        tokens=[],
        secret=None,
        local=True,
        endpoint="http://127.0.0.1:17171",
        modes=[],
        loaded_version="0.7.2",
        runtime_versions=[],
    )

    def command(args):
        state.commands.append(args)
        if args[0] == "ps":
            if "ppid=" in args:
                return f"1 321 {module.os.getuid()}"
            return f"Wed Sep 9 00:00:00 2026 {node} {state.spawns[-1][0][1]} start"
        if args[0] == "lsof":
            return f"p321\nfcwd\nn{vault}\n"
        if args[0] in {str(node), str(runtime_node)}:
            if args[1] == "-p":
                return str(runtime_node)
            return "v22.20.0"
        if args[1] == "context":
            return "unix:///test/docker.sock"
        return "29.4.0" if args[1] == "version" else "sha256:" + "b" * 64

    def popen(args, **options):
        state.running = True
        state.spawns.append((args, options))
        return SimpleNamespace(
            pid=321,
            poll=lambda: None if state.running else 0,
            terminate=lambda: setattr(state, "running", False),
            wait=lambda **kwargs: 0,
        )

    def capabilities(token):
        state.tokens.append(token)
        return {
            "protocolVersion": 1,
            "runnerVersion": "0.1.0",
            "engine": "29.4.0",
            "languages": ["kotlin"],
        }

    def runtime(cli_path, name, config):
        assert cli_path == cli and name == "vault"
        assert config["target"]["plugin_id"] == plugin_id
        assert config["version"] == state.loaded_version
        state.runtime_versions.append(config["version"])
        assert config["settings_sha256"] == module._sha(manager.settings.read_bytes())
        assert config["config_sha256"] == module._sha(manager.config.read_bytes())
        token = json.loads(manager.config.read_bytes())["token"]
        assert token not in json.dumps(config)
        mode = config["mode"]
        state.modes.append(mode)
        if mode == "disable":
            state.local = False
        elif state.secret and state.secret != token:
            raise WoonError("secret conflict")
        elif mode == "pair":
            state.local = False
            state.secret = token
        elif mode == "activate":
            state.local = True
        return {
            "status": "ok",
            "window_epoch": 101,
            "version": state.loaded_version,
            "local_enabled": state.local,
            "remote_enabled": False,
            "endpoint": state.endpoint,
            "secret_present": bool(state.secret),
            "secret_matches": state.secret == token,
        }

    monkeypatch.setattr(module, "_command", command)
    monkeypatch.setattr(module.subprocess, "Popen", popen)
    monkeypatch.setattr(
        module, "_listeners", lambda: [(321, "127.0.0.1:17171")] if state.running else []
    )
    monkeypatch.setattr(module, "_capabilities", capabilities)
    monkeypatch.setattr(module, "runtime_pairing", runtime)
    return SimpleNamespace(
        manager=manager,
        state=state,
        artifact=artifact,
        node=node,
        docker=docker,
        cli=cli,
        runtime=runtime,
    )


def start(c):
    plan = c.manager.start(c.artifact, c.node, c.docker)
    return c.manager.service._mutate(
        lambda: c.manager.start(
            c.artifact,
            c.node,
            c.docker,
            apply=True,
            expected_state=plan["expected_state"],
        )
    )


def test_start_preserves_config_reuses_owned_service_and_never_captures_token(
    companion, monkeypatch
):
    c = companion
    c.manager.config.parent.mkdir(parents=True, mode=0o700)
    original = b'{"port":17171,"token":"' + b"x" * 40 + b'", "extra": true}\n'
    c.manager.config.write_bytes(original)
    c.manager.config.chmod(0o600)
    monkeypatch.setenv("RCB_LOCAL_RUNNER_TOKEN", "must-not-override")
    first = start(c)
    again = start(c)
    assert first["ready"] and again["changed"] is False
    assert c.manager.config.read_bytes() == original
    assert len(c.state.spawns) == 1
    args, options = c.state.spawns[0]
    assert args[-1] == "start" and args[1].startswith(str(c.manager.root))
    assert args[0] == str(c.node.parent / "real-node") and args[0] != str(c.node)
    assert options["stdout"] == options["stderr"] == options["stdin"] == subprocess.DEVNULL
    assert "RCB_LOCAL_RUNNER_TOKEN" not in options["env"]
    assert options["env"]["RCB_CONTAINER_ENGINE"] == str(c.docker)
    assert not any("pull" in args or "run" in args for args in c.state.commands)
    assert "x" * 40 not in json.dumps(first)
    assert (c.manager.config.stat().st_mode & 0o777) == 0o600


@pytest.mark.parametrize("existing", [True, False])
def test_readiness_failure_stops_only_own_child_without_success_receipt(
    companion, monkeypatch, existing
):
    c = companion
    if existing:
        c.manager.config.parent.mkdir(parents=True, mode=0o700)
        c.manager.config.write_bytes(module._json({"port": 17171, "token": "k" * 40}))
        c.manager.config.chmod(0o600)
    before = module._current_file_bytes(c.manager.config)

    def unavailable(token):
        raise WoonError("not ready")

    monkeypatch.setattr(module, "_capabilities", unavailable)
    with pytest.raises(WoonError, match="not ready"):
        start(c)
    assert not c.state.running
    assert module._current_file_bytes(c.manager.config) == before
    assert not c.manager.state.exists()
    assert not list((c.manager.service._local / "receipts").glob("*.json"))


def test_stale_start_and_unmanaged_listener_do_not_send_token(companion, monkeypatch):
    c = companion
    plan = c.manager.start(c.artifact, c.node, c.docker)
    assert not c.manager.config.exists() and not c.manager.root.exists()
    c.manager.settings.write_text('{"remoteExecutionEnabled":false,"concurrent":true}')
    with pytest.raises(WoonError, match="plan changed"):
        c.manager.start(
            c.artifact, c.node, c.docker, apply=True, expected_state=plan["expected_state"]
        )
    monkeypatch.setattr(module, "_listeners", lambda: [(999, "*:17171")])
    with pytest.raises(WoonError, match="occupied"):
        c.manager.status()
    assert not c.state.spawns and not c.state.tokens


def test_pair_rereads_preserves_legacy_data_and_conflicting_secret(companion):
    c = companion
    start(c)
    c.state.secret = "different-secret"
    before = c.manager.settings.read_bytes()
    with pytest.raises(WoonError, match="secret conflict"):
        c.manager.pair(c.cli, "vault")
    assert c.manager.settings.read_bytes() == before and c.state.secret == "different-secret"
    c.state.secret = None
    plan = c.manager.pair(c.cli, "vault")
    result = c.manager.pair(c.cli, "vault", apply=True, expected_state=plan["expected_state"])
    assert result["runtime_pairing_verified"] is True
    assert c.state.modes[-3:] == ["pair", "activate", "read"]
    data = json.loads(c.manager.settings.read_bytes())
    assert data["custom"] == {"keep": 42} and data["kotlinPath"] == "/legacy"
    assert data["remoteExecutionEnabled"] is False and data["localExecutionEnabled"] is True
    assert (c.manager.vault / result["backup"]).read_bytes() == before
    assert c.state.secret not in json.dumps(result)
    replan = c.manager.pair(c.cli, "vault")
    assert (
        c.manager.pair(c.cli, "vault", apply=True, expected_state=replan["expected_state"])[
            "changed"
        ]
        is False
    )


@pytest.mark.parametrize("companion", ["manta"], indirect=True)
def test_pair_routes_through_manta_and_edits_only_its_run_scope(companion):
    c = companion
    start(c)
    plan = c.manager.pair(c.cli, "vault")
    assert plan["runtime"]["secret_present"] is False
    result = c.manager.pair(c.cli, "vault", apply=True, expected_state=plan["expected_state"])
    assert result["runtime_pairing_verified"] is True
    data = json.loads(c.manager.settings.read_bytes())
    assert c.manager.settings.parent.name == "manta" and data["locale"] == "ko"
    assert data["run"] == {
        "remoteExecutionEnabled": False,
        "localExecutionEnabled": True,
        "localRunnerEndpoint": c.state.endpoint,
        "kotlinPath": "/legacy",
        "custom": {"keep": 42},
    }
    assert "localExecutionEnabled" not in data
    assert "manta/data.json" in result["backup"]


def test_plugin_approvals_preserve_the_existing_companion_across_plugin_updates(companion):
    c = companion
    start(c)
    config_before = c.manager.config.read_bytes()
    state_before = c.manager.state.read_bytes()
    managed = Path(json.loads(state_before)["artifact"])
    artifact_before = managed.read_bytes()
    manifest = c.manager.settings.parent / "manifest.json"
    manifest.write_bytes(module._json({"id": "runnable-code-blocks", "version": "0.7.3"}))
    # A policy without the new field keeps its original exact-version restriction.
    with pytest.raises(WoonError, match="registered plugin release"):
        c.manager.pair(c.cli, "vault")
    assert not c.state.modes
    policy = {**c.manager.policy, "approved_plugin_versions": ["0.7.2", "0.7.3"]}
    c.manager = module.RunnableCompanionManager(
        c.manager.service, home=c.manager.config.parents[2], policy=policy
    )
    c.state.secret = json.loads(config_before)["token"]
    for version in ("0.7.2", "0.7.3"):
        manifest.write_bytes(module._json({"id": "runnable-code-blocks", "version": version}))
        c.state.loaded_version = version
        plan = c.manager.pair(c.cli, "vault")
        result = c.manager.pair(c.cli, "vault", apply=True, expected_state=plan["expected_state"])
        assert result["runtime_pairing_verified"] is True
        assert c.state.runtime_versions[-1] == version
    settings_before = c.manager.settings.read_bytes()
    modes_before = list(c.state.modes)
    manifest.write_bytes(module._json({"id": "runnable-code-blocks", "version": "0.7.4"}))
    with pytest.raises(WoonError, match="registered plugin release"):
        c.manager.pair(c.cli, "vault")
    assert c.state.modes == modes_before
    assert c.manager.settings.read_bytes() == settings_before
    assert c.manager.config.read_bytes() == config_before
    assert c.manager.state.read_bytes() == state_before and managed.read_bytes() == artifact_before
    assert c.state.secret == json.loads(config_before)["token"]
    assert len(c.state.spawns) == 1 and c.manager.policy["release"] == "0.7.2"
    assert c.manager.policy["artifact_sha256"] == module._sha(artifact_before)


@pytest.mark.parametrize("versions", [None, [], "0.7.2", [""], ["0.7.2", 3], ["0.7.2", "0.7.2"]])
def test_invalid_explicit_plugin_approvals_do_not_fall_back_to_the_release(companion, versions):
    c = companion
    with pytest.raises(WoonError, match="approved plugin versions"):
        module.RunnableCompanionManager(
            c.manager.service,
            home=c.manager.config.parents[2],
            policy={**c.manager.policy, "approved_plugin_versions": versions},
        )
    assert not c.state.spawns and not c.manager.config.exists()


@pytest.mark.parametrize("concurrent", [False, True])
def test_pair_failure_keeps_secret_pending_and_preserves_concurrent_edit(
    companion, monkeypatch, concurrent
):
    c = companion
    start(c)
    receipts_before = set((c.manager.service._local / "receipts").glob("*.json"))
    plan = c.manager.pair(c.cli, "vault")
    user_bytes = b'{"remoteExecutionEnabled":false,"user":"new edit"}'

    def fail_activate(cli, name, config):
        if config["mode"] == "activate":
            if concurrent:
                c.manager.settings.write_bytes(user_bytes)
            raise WoonError("window disconnected")
        return c.runtime(cli, name, config)

    monkeypatch.setattr(module, "runtime_pairing", fail_activate)
    with pytest.raises(WoonError, match="no success receipt"):
        c.manager.pair(c.cli, "vault", apply=True, expected_state=plan["expected_state"])
    assert c.state.secret is not None
    assert set((c.manager.service._local / "receipts").glob("*.json")) == receipts_before
    if concurrent:
        assert c.manager.settings.read_bytes() == user_bytes
    else:
        data = json.loads(c.manager.settings.read_bytes())
        assert data["localExecutionEnabled"] is False and data["remoteExecutionEnabled"] is False
        assert data["custom"] == {"keep": 42} and c.state.local is False
    attempt = next(
        json.loads(path.read_bytes())
        for path in (c.manager.root / "attempts").glob("*.json")
        if json.loads(path.read_bytes())["action"] == "pair-runnable-companion"
    )
    assert attempt["applied"] is False and attempt["secret_state"] == "may-be-paired"
    assert attempt["concurrent_settings_preserved"] is concurrent


def test_unexpected_child_listener_preserves_config_and_records_failure(companion, monkeypatch):
    c = companion
    monkeypatch.setattr(
        module, "_listeners", lambda: [(999, "127.0.0.1:17171")] if c.state.spawns else []
    )
    with pytest.raises(WoonError, match="unresolved process/port"):
        start(c)
    assert c.manager.config.exists() and not c.manager.state.exists()
    assert not list((c.manager.service._local / "receipts").glob("*.json"))
    attempt = json.loads(next((c.manager.root / "attempts").glob("*.json")).read_bytes())
    assert attempt["status"] == "failed" and attempt["child_stopped"] is True
    assert attempt["remaining_listeners"] == [[999, "127.0.0.1:17171"]]
    assert attempt["config_preserved"] is True
    assert not c.state.tokens


def test_orphan_recovery_pins_identity_and_signals_only_reviewed_pid(companion, monkeypatch):
    c = companion
    artifact = c.manager.root / f"runner-{c.manager.policy['artifact_sha256']}.mjs"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(c.artifact.read_bytes())
    c.state.spawns.append(([str(c.node), str(artifact), "start"], {}))
    c.state.running = True
    signals = []
    monkeypatch.setattr(module, "_process_exists", lambda pid: c.state.running)

    def signal_pid(pid, sig):
        signals.append((pid, sig))
        c.state.running = False

    monkeypatch.setattr(module.os, "kill", signal_pid)
    plan = c.manager.recover_orphan(321)
    assert not signals and not c.state.tokens
    with pytest.raises(WoonError, match="identity changed"):
        c.manager.recover_orphan(321, apply=True, expected_state="old-state")
    assert not signals
    result = c.manager.recover_orphan(321, apply=True, expected_state=plan["expected_state"])
    assert signals == [(321, module.signal.SIGTERM)]
    assert result["process_exit_verified"] is True and result["port_clear"] is True
    assert not c.manager.config.exists() and not c.manager.state.exists()
    assert not c.state.tokens and result["http_requests"] == 0


def test_late_pair_receipt_failure_removes_false_success(companion, monkeypatch):
    c = companion
    start(c)
    plan = c.manager.pair(c.cli, "vault")
    receipts_before = set((c.manager.service._local / "receipts").glob("*.json"))
    original = module._atomic_write

    def fail_receipt(path, content):
        original(path, content)
        if path.parent.name == "receipts":
            raise OSError("late disk failure")

    monkeypatch.setattr(module, "_atomic_write", fail_receipt)
    with pytest.raises(WoonError, match="no success receipt"):
        c.manager.pair(c.cli, "vault", apply=True, expected_state=plan["expected_state"])
    assert set((c.manager.service._local / "receipts").glob("*.json")) == receipts_before
    assert json.loads(c.manager.settings.read_bytes())["localExecutionEnabled"] is False


def test_companion_cli_requires_explicit_paths_and_plan_hash(tmp_path, monkeypatch):
    from io import StringIO

    from woon_core import cli

    calls = []

    class Service:
        def __init__(self, vault):
            assert vault == tmp_path

        def start_runnable_companion(self, *args, **kwargs):
            calls.append((args, kwargs))
            return {"applied": False}

    monkeypatch.setattr(cli, "ObsidianPluginService", Service)
    cli.run(
        [
            "knowledge",
            "obsidian-plugin",
            "start-runnable-companion",
            "--vault",
            str(tmp_path),
            "--companion-cli",
            "/artifact",
            "--node-cli",
            "/node",
            "--docker-cli",
            "/docker",
            "--apply",
            "--expected-state",
            "pin",
        ],
        StringIO(),
    )
    assert calls == [
        (
            (Path("/artifact"), Path("/node"), Path("/docker")),
            {"apply": True, "expected_state": "pin"},
        )
    ]


@pytest.fixture
def managed_stop(companion, monkeypatch):
    c = companion
    start(c)
    c.signals = []
    monkeypatch.setattr(module, "_require_idle_process", lambda pid, docker: None)
    monkeypatch.setattr(module, "_process_exists", lambda pid: c.state.running)

    def stop_pid(pid, sig):
        c.signals.append((pid, sig))
        c.state.running = False

    monkeypatch.setattr(module.os, "kill", stop_pid)
    return c


def test_managed_stop_preserves_restore_inputs_and_requires_reviewed_state(managed_stop):
    c = managed_stop
    files = (c.manager.config, c.manager.settings, c.manager.state)
    before = {path: path.read_bytes() for path in files}
    plan = c.manager.stop(321, c.docker)
    assert not c.signals
    with pytest.raises(WoonError, match="plan changed"):
        c.manager.stop(321, c.docker, apply=True, expected_state="stale")
    assert not c.signals
    result = c.manager.service._mutate(
        lambda: c.manager.stop(321, c.docker, apply=True, expected_state=plan["expected_state"])
    )
    assert c.signals == [(321, module.signal.SIGTERM)]
    assert result["process_exit_verified"] and result["port_clear"]
    assert {path: path.read_bytes() for path in files} == before
    receipt = c.manager.service._local / "receipts" / f"{result['receipt_id']}.json"
    assert json.loads(receipt.read_bytes()) == result
    assert result["previous_receipt_id"] == json.loads(before[c.manager.state])["receipt_id"]
    assert c.manager.status()["ready"] is False


@pytest.mark.parametrize("change", ["receipt", "settings", "busy-before-signal"])
def test_managed_stop_rejects_changed_ownership_or_work(managed_stop, monkeypatch, change):
    c = managed_stop
    plan = c.manager.stop(321, c.docker)
    if change == "receipt":
        receipt = c.manager.service._local / "receipts" / f"{plan['receipt_id']}.json"
        receipt.write_bytes(receipt.read_bytes() + b" ")
    elif change == "settings":
        c.manager.settings.write_bytes(c.manager.settings.read_bytes() + b" ")
    else:
        checks = []

        def becomes_busy(pid, docker):
            checks.append(pid)
            if len(checks) == 2:
                raise WoonError("new work appeared")

        monkeypatch.setattr(module, "_require_idle_process", becomes_busy)
    with pytest.raises(WoonError):
        c.manager.stop(321, c.docker, apply=True, expected_state=plan["expected_state"])
    assert c.state.running and not c.signals
    assert len(list((c.manager.service._local / "receipts").glob("*.json"))) == 1


def test_managed_stop_late_receipt_failure_retains_restore_state_without_false_success(
    managed_stop, monkeypatch
):
    c = managed_stop
    before = {p: p.read_bytes() for p in (c.manager.config, c.manager.settings, c.manager.state)}
    plan = c.manager.stop(321, c.docker)
    original = module._atomic_write

    def fail_receipt(path, content):
        original(path, content)
        if path.parent.name == "receipts":
            raise OSError("late receipt failure")

    monkeypatch.setattr(module, "_atomic_write", fail_receipt)
    with pytest.raises(WoonError, match="do not retry"):
        c.manager.stop(321, c.docker, apply=True, expected_state=plan["expected_state"])
    assert c.signals == [(321, module.signal.SIGTERM)]
    assert {p: p.read_bytes() for p in before} == before
    assert len(list((c.manager.service._local / "receipts").glob("*.json"))) == 1
    attempts = [json.loads(p.read_bytes()) for p in (c.manager.root / "attempts").glob("*.json")]
    stopped = next(a for a in attempts if a["action"] == "stop-runnable-companion")
    assert stopped["status"] == "failed" and stopped["signal_sent"] is True


@pytest.mark.parametrize("busy", ["child", "connection", "container", "unknown", None])
def test_idle_observation_blocks_unfinished_work_or_unavailable_evidence(monkeypatch, busy):
    def command(args):
        if args[0] == "ps":
            return {"child": "777 321", "unknown": ""}.get(busy, "321 1")
        assert args == ["/docker", "ps", "--all", "--quiet", "--filter", "name=^/rcb-"]
        return "container-id" if busy == "container" else ""

    monkeypatch.setattr(module, "_command", command)
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0 if busy == "connection" else 1,
            stdout="p321\nn127.0.0.1:17171->127.0.0.1:12345" if busy == "connection" else "",
            stderr="",
        ),
    )
    if busy is None:
        module._require_idle_process(321, Path("/docker"))
    else:
        with pytest.raises(WoonError):
            module._require_idle_process(321, Path("/docker"))


def test_pair_does_not_touch_a_different_selected_secret(companion):
    c = companion
    start(c)
    settings = json.loads(c.manager.settings.read_bytes())
    settings["localRunnerSecretId"] = "user-selected-secret"
    before = module._json(settings)
    c.manager.settings.write_bytes(before)
    with pytest.raises(WoonError, match="default managed secret"):
        c.manager.pair(c.cli, "vault")
    assert c.manager.settings.read_bytes() == before and not c.state.modes


def test_managed_stop_cli_keeps_exact_pid_docker_and_preview_hash(tmp_path, monkeypatch):
    from io import StringIO

    from woon_core import cli

    calls = []

    class Service:
        def __init__(self, vault):
            assert vault == tmp_path

        def stop_runnable_companion(self, *args, **kwargs):
            calls.append((args, kwargs))
            return {"applied": False}

    monkeypatch.setattr(cli, "ObsidianPluginService", Service)
    cli.run(
        [
            "knowledge",
            "obsidian-plugin",
            "stop-runnable-companion",
            "--vault",
            str(tmp_path),
            "--pid",
            "321",
            "--docker-cli",
            "/docker",
            "--apply",
            "--expected-state",
            "pin",
        ],
        StringIO(),
    )
    assert calls == [((321, Path("/docker")), {"apply": True, "expected_state": "pin"})]
