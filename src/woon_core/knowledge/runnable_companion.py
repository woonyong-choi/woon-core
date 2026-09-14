"""Explicit loopback companion lifecycle; never submit source or pull images."""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import re
import secrets
import signal
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from woon_core.errors import WoonError
from woon_core.io import load_yaml
from woon_core.knowledge.obsidian_plugins import (
    REQUIRED_ASSETS,
    RUNNABLE_CODE_BLOCKS_ID,
    _atomic_write,
    _current_file_bytes,
    _require_unchanged_file,
    _require_vault_local_directory,
    _require_vault_local_file,
    _restore_file_if_owned,
)
from woon_core.knowledge.runnable_pairing import runtime_pairing
from woon_core.registry import Registry
from woon_core.workspace import discover

if TYPE_CHECKING:
    from woon_core.knowledge.obsidian_plugins import ObsidianPluginService


def _sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _json(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()


def _command(arguments: list[str]) -> str:
    try:
        result = subprocess.run(arguments, capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise WoonError("companion prerequisite command unavailable") from error
    if result.returncode:
        raise WoonError("companion prerequisite unavailable; no runtime installation or image pull")
    return result.stdout.strip()


def _listeners() -> list[tuple[int, str]]:
    try:
        result = subprocess.run(
            ["lsof", "-nP", "-iTCP:17171", "-sTCP:LISTEN", "-Fpn"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise WoonError("cannot establish ownership of the companion port") from error
    if result.returncode not in {0, 1} or (result.returncode == 1 and result.stderr.strip()):
        raise WoonError("cannot establish ownership of the companion port")
    entries: list[tuple[int, str]] = []
    pid: int | None = None
    for line in result.stdout.splitlines():
        if line.startswith("p") and line[1:].isdigit():
            pid = int(line[1:])
        elif line.startswith("n") and pid is not None:
            entries.append((pid, line[1:]))
    return entries


def _process_signature(pid: int, artifact: Path) -> str:
    value = _command(["ps", "-p", str(pid), "-o", "lstart=", "-o", "command="])
    if not value.endswith(f" {artifact} start"):
        raise WoonError("companion process does not match the managed artifact")
    return _sha(value.encode())


def _capabilities(token: str) -> dict[str, Any]:
    # Fixed transport and path: redirects and public gateway requests are impossible.
    connection = http.client.HTTPConnection("127.0.0.1", 17171, timeout=5)
    try:
        connection.request("GET", "/v1/capabilities", headers={"Authorization": f"Bearer {token}"})
        response = connection.getresponse()
        raw = response.read(65537)
        if response.status != 200 or len(raw) > 65536:
            raise WoonError("authenticated loopback capabilities are unavailable")
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise WoonError("companion capabilities are not a JSON object")
        return payload
    except (OSError, ValueError, http.client.HTTPException) as error:
        raise WoonError("authenticated loopback capabilities are unavailable") from error
    finally:
        connection.close()


def _process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError as error:
        raise WoonError("cannot verify companion process ownership") from error
    return True


def _require_idle_process(pid: int, docker: Path) -> None:
    """Observe quiescence without cancelling jobs; callers also hold the UI Run pause."""
    processes = _command(["ps", "-axo", "pid=,ppid="])
    rows = [line.split() for line in processes.splitlines()]
    if not rows or any(len(row) != 2 or not all(value.isdigit() for value in row) for row in rows):
        raise WoonError("cannot verify companion child inactivity")
    if any(row[1] == str(pid) for row in rows):
        raise WoonError("managed companion has child work; finish it before stopping")
    try:
        sockets = subprocess.run(
            ["lsof", "-nP", "-a", "-p", str(pid), "-iTCP", "-sTCP:ESTABLISHED", "-Fpn"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise WoonError("cannot verify companion connection inactivity") from error
    if sockets.returncode not in {0, 1} or sockets.stderr.strip():
        raise WoonError("cannot verify companion connection inactivity")
    if sockets.stdout.strip():
        raise WoonError("managed companion has an open connection; finish it before stopping")
    if _command([str(docker), "ps", "--all", "--quiet", "--filter", "name=^/rcb-"]):
        raise WoonError("Runnable containers remain; preserve them and finish existing work")


class RunnableCompanionManager:
    """All durable writes are wrapped by the existing Obsidian adapter lock."""

    def __init__(
        self,
        service: ObsidianPluginService,
        *,
        home: Path | None = None,
        policy: dict[str, Any] | None = None,
    ) -> None:
        self.service = service
        self.vault = service._vault
        self.root = service._local / "companion"
        self.config = (home or Path.home()) / ".config/runnable-code-blocks/local-runner.json"
        self.settings = self.vault / ".obsidian/plugins" / RUNNABLE_CODE_BLOCKS_ID / "data.json"
        self.state = self.root / "state.json"
        service._require_mutation_boundary()
        if self.root.exists() or self.root.is_symlink():
            _require_vault_local_directory(self.vault, self.root, "companion local state")
        if policy is None:
            workspace = discover("")
            path = Registry.load(workspace.root).resolve(
                workspace.root, "repo://core/config/runnable-companion.yaml", must_exist=True
            )
            policy = load_yaml(path)
        if (
            policy.get("version") != 1
            or policy.get("host") != "127.0.0.1"
            or policy.get("port") != 17171
            or policy.get("protocol_version") != 1
            or re.fullmatch(r"[0-9a-f]{64}", str(policy.get("artifact_sha256"))) is None
            or not isinstance(policy.get("release"), str)
            or policy.get("required_language") != "kotlin"
            or re.fullmatch(
                r"docker\.io/[a-z0-9/.-]+@sha256:[0-9a-f]{64}", str(policy.get("required_image"))
            )
            is None
        ):
            raise WoonError("unsupported registered loopback companion policy")
        plugin_versions = policy.get("approved_plugin_versions", [policy["release"]])
        if (
            not isinstance(plugin_versions, list)
            or not plugin_versions
            or any(
                not isinstance(value, str) or not value or value != value.strip()
                for value in plugin_versions
            )
            or len(set(plugin_versions)) != len(plugin_versions)
        ):
            raise WoonError("companion policy requires distinct exact approved plugin versions")
        self.plugin_versions = tuple(plugin_versions)
        self.policy = policy

    def _configuration(self) -> tuple[bytes | None, dict[str, Any] | None]:
        # Never let the upstream CLI replace a malformed or differently configured file.
        for path in (self.config.parent.parent, self.config.parent, self.config):
            if path.is_symlink():
                raise WoonError("companion configuration must not use symlinks")
        if not self.config.exists():
            return None, None
        if not self.config.is_file():
            raise WoonError("companion configuration must be a regular owner-only file")
        stat = self.config.stat()
        if stat.st_uid != os.getuid() or stat.st_mode & 0o077:
            raise WoonError("existing companion config is not owner-only; preserve and review")
        raw = self.config.read_bytes()
        try:
            config = json.loads(raw)
        except ValueError as error:
            raise WoonError("existing companion config is invalid; preserve and review") from error
        if (
            not isinstance(config, dict)
            or type(config.get("port")) is not int
            or config["port"] != 17171
            or not isinstance(config.get("token"), str)
            or re.fullmatch(r"[A-Za-z0-9_-]{32,256}", config["token"]) is None
        ):
            raise WoonError("existing companion config is incompatible; preserve and review")
        return raw, config

    def _disk_policy(self) -> bytes:
        _require_vault_local_file(self.vault, self.settings, "Runnable settings")
        raw = self.settings.read_bytes()
        try:
            config = json.loads(raw)
        except ValueError as error:
            raise WoonError("Runnable settings are invalid") from error
        if not isinstance(config, dict) or config.get("remoteExecutionEnabled") is not False:
            raise WoonError("disable remote execution through its policy adapter first")
        return raw

    def _ready(self, config: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        pid = state.get("pid")
        if type(pid) is not int or _listeners() != [(pid, "127.0.0.1:17171")]:
            raise WoonError("companion listener ownership changed; no token sent")
        artifact = Path(state.get("artifact", ""))
        if (
            artifact != self.root / f"runner-{self.policy['artifact_sha256']}.mjs"
            or artifact.is_symlink()
            or not artifact.is_file()
            or _sha(artifact.read_bytes()) != self.policy["artifact_sha256"]
            or _process_signature(pid, artifact) != state.get("process_signature")
            or state.get("token_fingerprint") != _sha(config["token"].encode())[:12]
        ):
            raise WoonError("companion ownership receipt does not match; no token sent")
        capabilities = _capabilities(config["token"])
        languages = capabilities.get("languages")
        if (
            capabilities.get("protocolVersion") != self.policy["protocol_version"]
            or not isinstance(languages, list)
            or not all(isinstance(item, str) for item in languages)
            or self.policy["required_language"] not in languages
            or not isinstance(capabilities.get("runnerVersion"), str)
        ):
            raise WoonError("companion required capabilities are unavailable; no image pull")
        return {
            key: capabilities.get(key)
            for key in (
                "protocolVersion",
                "runnerVersion",
                "engine",
                "languages",
            )
        }

    def status(self) -> dict[str, Any]:
        raw, config = self._configuration()
        settings = self._disk_policy()
        listeners = _listeners()
        if self.state.exists() or self.state.is_symlink():
            _require_vault_local_file(self.vault, self.state, "companion state")
        state_bytes = _current_file_bytes(self.state)
        result: dict[str, Any] = {
            "config_sha256": _sha(raw) if raw is not None else None,
            "settings_sha256": _sha(settings),
            "managed_state_sha256": _sha(state_bytes) if state_bytes is not None else None,
            "endpoint": "http://127.0.0.1:17171",
            "ready": False,
            "source_requests": 0,
            "image_pulls": 0,
        }
        if not listeners:
            return result
        if config is None or state_bytes is None:
            raise WoonError("companion port is occupied without a matching managed receipt")
        try:
            state = json.loads(state_bytes)
        except ValueError as error:
            raise WoonError("invalid companion state; preserve and review") from error
        if not isinstance(state, dict) or not isinstance(state.get("receipt_id"), str):
            raise WoonError("invalid companion state; preserve and review")
        result["capabilities"] = self._ready(config, state)
        result.update(ready=True, pid=state["pid"], receipt_id=state["receipt_id"])
        return result

    def start(
        self,
        artifact: Path,
        node: Path,
        docker: Path,
        *,
        apply: bool = False,
        expected_state: str | None = None,
    ) -> dict[str, Any]:
        for executable in (node, docker):
            if not executable.is_absolute() or not executable.is_file():
                raise WoonError("companion requires explicit existing Node and Docker CLI paths")
        if (
            not artifact.is_absolute()
            or artifact.is_symlink()
            or not artifact.is_file()
            or _sha(artifact.read_bytes()) != self.policy["artifact_sha256"]
        ):
            raise WoonError("companion artifact does not match the registered release SHA-256")
        # Volta and other shims can fork instead of exec. Pin and launch the actual
        # Node binary so Popen.pid owns the listener and rollback targets that PID.
        resolved_node = Path(_command([str(node), "-p", "process.execPath"]))
        if not resolved_node.is_absolute() or not resolved_node.is_file():
            raise WoonError("Node shim did not resolve an existing absolute executable")
        resolved_node = resolved_node.resolve()
        if _command([str(resolved_node), "-p", "process.execPath"]) != str(resolved_node):
            raise WoonError("resolved Node executable is still a shim; no companion started")
        node_hash = _sha(resolved_node.read_bytes())
        node_version = _command([str(resolved_node), "--version"])
        if (
            re.fullmatch(r"v\d+\.\d+\.\d+", node_version) is None
            or int(node_version[1:].split(".")[0]) < 22
        ):
            raise WoonError("an existing Node.js 22 or newer is required; no installation")
        docker_version = _command([str(docker), "version", "--format", "{{.Server.Version}}"])
        docker_endpoint = _command(
            [
                str(docker),
                "context",
                "inspect",
                "--format",
                "{{.Endpoints.docker.Host}}",
            ]
        )
        if not docker_endpoint.startswith("unix:///") or (
            os.environ.get("DOCKER_HOST") and not os.environ["DOCKER_HOST"].startswith("unix:///")
        ):
            raise WoonError("companion requires an existing local Unix-socket Docker engine")
        image_id = _command(
            [str(docker), "image", "inspect", self.policy["required_image"], "--format", "{{.Id}}"]
        )
        if re.fullmatch(r"sha256:[0-9a-f]{64}", image_id) is None:
            raise WoonError("required image identity could not be verified; no image pull")
        plan = {
            **self.status(),
            "action": "start-runnable-companion",
            "artifact_sha256": self.policy["artifact_sha256"],
            "release": self.policy["release"],
            "node": str(node),
            "resolved_node": str(resolved_node),
            "node_sha256": node_hash,
            "node_version": node_version,
            "docker": str(docker),
            "docker_version": docker_version,
            "docker_endpoint": docker_endpoint,
            "image_id": image_id,
            "policy_sha256": _sha(_json(self.policy)),
        }
        digest = _sha(_json(plan))
        plan.update(expected_state=digest, applied=False)
        if not apply:
            return plan
        if expected_state != digest:
            raise WoonError("companion plan changed or expected-state is missing; replan")
        if plan["ready"]:
            return {**plan, "applied": True, "changed": False}
        before_config, config = self._configuration()
        before_settings = self._disk_policy()
        if (_sha(before_config) if before_config is not None else None) != plan["config_sha256"]:
            raise WoonError("companion config changed since plan")
        if _sha(before_settings) != plan["settings_sha256"]:
            raise WoonError("Runnable settings changed since plan")
        created_config: bytes | None = None
        child: subprocess.Popen[bytes] | None = None
        receipt_path: Path | None = None
        receipt_bytes: bytes | None = None
        before_state = _current_file_bytes(self.state)
        if (_sha(before_state) if before_state is not None else None) != plan[
            "managed_state_sha256"
        ]:
            raise WoonError("companion managed state changed since plan")
        receipt_id = self.service._receipt_id()
        attempt_path = self.root / "attempts" / f"{receipt_id}.json"
        attempt: dict[str, Any] = {
            "action": "start-runnable-companion",
            "status": "preparing",
            "applied": False,
            "receipt_id": receipt_id,
            "pid": None,
            "expected_state": digest,
            "config_before_sha256": plan["config_sha256"],
            "resolved_node": str(resolved_node),
            "source_requests": 0,
            "image_pulls": 0,
        }
        _atomic_write(attempt_path, _json(attempt))
        try:
            if config is None:
                self.config.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                if self.config.parent.stat().st_mode & 0o077:
                    raise WoonError("new token requires an owner-only companion config directory")
                config = {"port": 17171, "token": secrets.token_urlsafe(32)}
                new_config = _json(config)
                descriptor = os.open(self.config, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                created_config = new_config
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(created_config)
                    stream.flush()
                    os.fsync(stream.fileno())
            _require_unchanged_file(
                self.config, created_config or before_config, "companion config"
            )
            managed = self.root / f"runner-{self.policy['artifact_sha256']}.mjs"
            if managed.exists():
                if (
                    managed.is_symlink()
                    or _sha(managed.read_bytes()) != self.policy["artifact_sha256"]
                ):
                    raise WoonError("managed companion artifact changed; preserve and review")
            else:
                _atomic_write(managed, artifact.read_bytes())
            if (
                _sha(managed.read_bytes()) != self.policy["artifact_sha256"]
                or _listeners()
                or _sha(resolved_node.read_bytes()) != node_hash
            ):
                raise WoonError("companion artifact or port changed before start")
            environment = {
                key: value
                for key, value in os.environ.items()
                if not key.startswith("RCB_") and key != "NODE_OPTIONS"
            }
            environment["RCB_CONTAINER_ENGINE"] = str(docker)
            # Upstream prints its token. No pipe, log, argv, or receipt may capture it.
            child = subprocess.Popen(
                [str(resolved_node), str(managed), "start"],
                cwd=self.vault,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            attempt.update(status="spawned", pid=child.pid, artifact=str(managed))
            _atomic_write(attempt_path, _json(attempt))
            deadline = time.monotonic() + 12
            while not _listeners() and child.poll() is None and time.monotonic() < deadline:
                time.sleep(0.2)
            state = {
                "pid": child.pid,
                "artifact": str(managed),
                "process_signature": _process_signature(child.pid, managed),
                "token_fingerprint": _sha(config["token"].encode())[:12],
                "receipt_id": receipt_id,
            }
            capabilities = self._ready(config, state)
            _require_unchanged_file(
                self.config, created_config or before_config, "companion config"
            )
            _require_unchanged_file(self.settings, before_settings, "Runnable settings")
            _require_unchanged_file(self.state, before_state, "companion state")
            receipt = {
                **plan,
                **state,
                "applied": True,
                "changed": True,
                "ready": True,
                "config_sha256": _sha(self.config.read_bytes()),
                "capabilities": capabilities,
                "token_created": created_config is not None,
            }
            receipt_bytes = _json(receipt)
            receipt_path = self.service._local / "receipts" / f"{state['receipt_id']}.json"
            _atomic_write(attempt_path, _json({**attempt, "status": "verified-awaiting-receipt"}))
            _atomic_write(receipt_path, receipt_bytes)
            _atomic_write(self.state, receipt_bytes)
            self.status()
            return receipt
        except Exception as error:
            stopped = child is None or child.poll() is not None
            if not stopped and child is not None:
                try:
                    child.terminate()
                    child.wait(timeout=5)
                    stopped = True
                except (OSError, subprocess.TimeoutExpired):
                    stopped = child.poll() is not None
            remaining: list[tuple[int, str]] | None
            try:
                remaining = _listeners()
            except WoonError:
                remaining = None
            port_clear = remaining == []
            if receipt_bytes is not None:
                if _current_file_bytes(self.state) == receipt_bytes:
                    if before_state is None:
                        self.state.unlink()
                    else:
                        _atomic_write(self.state, before_state)
                if receipt_path is not None and _current_file_bytes(receipt_path) == receipt_bytes:
                    receipt_path.unlink()
            if (
                stopped
                and port_clear
                and created_config is not None
                and _current_file_bytes(self.config) == created_config
            ):
                self.config.unlink()
            attempt.update(
                status="failed",
                child_stopped=stopped,
                remaining_listeners=remaining,
                config_preserved=self.config.exists(),
                port_clear=port_clear,
            )
            _atomic_write(attempt_path, _json(attempt))
            if not stopped or not port_clear:
                raise WoonError(
                    f"companion start failed with unresolved process/port; inspect attempt "
                    f"{receipt_id}; config preserved; do not retry start"
                ) from error
            raise

    def stop(
        self,
        pid: int,
        docker: Path,
        *,
        apply: bool = False,
        expected_state: str | None = None,
    ) -> dict[str, Any]:
        """Stop only a receipt-owned idle PID, preserving config, pairing and prior state.

        The operator pauses new Run requests through the stop/start handoff.
        OS/container quiescence is checked again immediately before SIGTERM;
        it is not presented as an unsupported server-side active-job counter.
        """
        status = self.status()
        if not status["ready"] or status.get("pid") != pid or pid <= 1:
            raise WoonError("managed stop requires the exact ready companion PID")
        before_state = self.state.read_bytes()
        state = json.loads(before_state)
        receipt_id = state.get("receipt_id", "")
        if not isinstance(receipt_id, str) or not re.fullmatch(r"[A-Za-z0-9-]+", receipt_id):
            raise WoonError("managed stop requires a valid original start receipt")
        receipt_path = self.service._local / "receipts" / f"{receipt_id}.json"
        _require_vault_local_file(self.vault, receipt_path, "companion start receipt")
        if (
            receipt_path.read_bytes() != before_state
            or state.get("action") != "start-runnable-companion"
            or state.get("applied") is not True
            or state.get("ready") is not True
        ):
            raise WoonError("managed stop requires the unchanged successful start receipt")
        if not docker.is_absolute() or not docker.is_file() or str(docker) != state.get("docker"):
            raise WoonError("managed stop requires the original Docker CLI")
        endpoint = _command(
            [str(docker), "context", "inspect", "--format", "{{.Endpoints.docker.Host}}"]
        )
        if (
            not endpoint.startswith("unix:///")
            or endpoint != state.get("docker_endpoint")
            or (os.environ.get("DOCKER_HOST") and os.environ["DOCKER_HOST"] != endpoint)
        ):
            raise WoonError("managed stop must use the original local Docker engine")
        identity = _command(
            ["ps", "-p", str(pid), "-o", "ppid=", "-o", "pgid=", "-o", "uid="]
        ).split()
        if (
            len(identity) != 3
            or not all(x.isdigit() for x in identity)
            or int(identity[2]) != os.getuid()
        ):
            raise WoonError("managed companion is not owned by the current user")
        cwd = _command(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"])
        if [line[1:] for line in cwd.splitlines() if line.startswith("n")] != [str(self.vault)]:
            raise WoonError("managed companion working directory changed")
        before_config, _ = self._configuration()
        before_settings = self._disk_policy()
        if (
            _sha(before_state) != status["managed_state_sha256"]
            or _sha(before_config or b"") != status["config_sha256"]
            or _sha(before_settings) != status["settings_sha256"]
        ):
            raise WoonError("managed stop state changed during preparation")
        _require_idle_process(pid, docker)
        plan = {
            **status,
            "action": "stop-runnable-companion",
            "artifact": state["artifact"],
            "artifact_sha256": self.policy["artifact_sha256"],
            "process_signature": state["process_signature"],
            "owner_uid": int(identity[2]),
            "parent_pid": int(identity[0]),
            "pgid": int(identity[1]),
            "cwd": str(self.vault),
            "docker": str(docker),
            "docker_endpoint": endpoint,
            "docker_sha256": _sha(docker.read_bytes()),
            "policy_sha256": _sha(_json(self.policy)),
            "previous_receipt_id": receipt_id,
            "idle_observation": "no-children-connections-containers",
            "signal": "SIGTERM",
            "config_preserved": True,
            "settings_preserved": True,
        }
        digest = _sha(_json(plan))
        plan.update(expected_state=digest, applied=False)
        if not apply:
            return plan
        if expected_state != digest:
            raise WoonError("managed stop plan changed or expected-state is missing; replan")
        operation_id = self.service._receipt_id()
        attempt_path = self.root / "attempts" / f"{operation_id}.json"
        output = self.service._local / "receipts" / f"{operation_id}.json"
        attempt = {**plan, "receipt_id": operation_id, "status": "pending", "signal_sent": False}
        _atomic_write(attempt_path, _json(attempt))
        written: bytes | None = None
        try:
            _require_unchanged_file(self.state, before_state, "companion state")
            _require_unchanged_file(receipt_path, before_state, "companion start receipt")
            _require_unchanged_file(self.config, before_config, "companion config")
            _require_unchanged_file(self.settings, before_settings, "Runnable settings")
            if (
                _sha(docker.read_bytes()) != plan["docker_sha256"]
                or _command(
                    [str(docker), "context", "inspect", "--format", "{{.Endpoints.docker.Host}}"]
                )
                != endpoint
            ):
                raise WoonError("managed stop Docker engine changed before signal")
            if _process_signature(pid, Path(state["artifact"])) != state[
                "process_signature"
            ] or _listeners() != [(pid, "127.0.0.1:17171")]:
                raise WoonError("managed companion identity changed before signal")
            _require_idle_process(pid, docker)
            os.kill(pid, signal.SIGTERM)
            attempt.update(status="signal-sent", signal_sent=True)
            _atomic_write(attempt_path, _json(attempt))
            deadline = time.monotonic() + 5
            while _process_exists(pid) and time.monotonic() < deadline:
                time.sleep(0.1)
            if _process_exists(pid) or _listeners():
                raise WoonError(
                    "managed companion exit or port release unverified; no further signal"
                )
            _require_unchanged_file(self.config, before_config, "companion config")
            _require_unchanged_file(self.settings, before_settings, "Runnable settings")
            _require_unchanged_file(self.state, before_state, "companion state")
            result = {
                **plan,
                "receipt_id": operation_id,
                "applied": True,
                "ready": False,
                "process_exit_verified": True,
                "port_clear": True,
            }
            _atomic_write(attempt_path, _json({**attempt, "status": "verified-awaiting-receipt"}))
            written = _json(result)
            _atomic_write(output, written)
            return result
        except Exception as error:
            if written is not None and _current_file_bytes(output) == written:
                output.unlink()
            _atomic_write(attempt_path, _json({**attempt, "status": "failed", "applied": False}))
            raise WoonError(
                f"managed stop failed; inspect attempt {operation_id}; do not retry"
            ) from error

    def recover_orphan(
        self,
        pid: int,
        *,
        apply: bool = False,
        expected_state: str | None = None,
    ) -> dict[str, Any]:
        """Review and SIGTERM one orphan of the exact managed artifact; never a process group."""
        if pid <= 1 or _listeners() != [(pid, "127.0.0.1:17171")]:
            raise WoonError("orphan recovery requires the exact sole loopback listener PID")
        if self.state.exists() or self.state.is_symlink():
            raise WoonError("a managed state exists; orphan recovery must not stop this service")
        artifact = self.root / f"runner-{self.policy['artifact_sha256']}.mjs"
        _require_vault_local_file(self.vault, artifact, "orphan managed artifact")
        if _sha(artifact.read_bytes()) != self.policy["artifact_sha256"]:
            raise WoonError("orphan artifact does not match the registered release")
        signature = _process_signature(pid, artifact)
        identity = _command(["ps", "-p", str(pid), "-o", "ppid=", "-o", "pgid=", "-o", "uid="])
        fields = identity.split()
        if (
            len(fields) != 3
            or not all(value.isdigit() for value in fields)
            or int(fields[0]) != 1
            or int(fields[2]) != os.getuid()
        ):
            raise WoonError("target is not an orphan owned by the current user")
        cwd = _command(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"])
        if [line[1:] for line in cwd.splitlines() if line.startswith("n")] != [str(self.vault)]:
            raise WoonError("orphan working directory does not match the exact Vault")
        config_bytes, _ = self._configuration()
        settings_bytes = self._disk_policy()
        plan = {
            "action": "recover-runnable-companion-orphan",
            "pid": pid,
            "pgid": int(fields[1]),
            "parent_pid": 1,
            "owner_uid": os.getuid(),
            "process_signature": signature,
            "artifact": str(artifact),
            "artifact_sha256": self.policy["artifact_sha256"],
            "cwd": str(self.vault),
            "config_sha256": _sha(config_bytes) if config_bytes else None,
            "settings_sha256": _sha(settings_bytes),
            "signal": "SIGTERM",
            "source_requests": 0,
            "image_pulls": 0,
            "http_requests": 0,
        }
        digest = _sha(_json(plan))
        plan.update(expected_state=digest, applied=False)
        if not apply:
            return plan
        if expected_state != digest:
            raise WoonError("orphan identity changed or expected-state is missing; replan")
        receipt_id = self.service._receipt_id()
        attempt_path = self.root / "attempts" / f"{receipt_id}.json"
        receipt_path = self.service._local / "receipts" / f"{receipt_id}.json"
        receipt_bytes: bytes | None = None
        attempt = {**plan, "receipt_id": receipt_id, "status": "pending", "signal_sent": False}
        _atomic_write(attempt_path, _json(attempt))
        try:
            if _process_signature(pid, artifact) != signature or _listeners() != [
                (pid, "127.0.0.1:17171")
            ]:
                raise WoonError("orphan identity changed before signal; no process stopped")
            os.kill(pid, signal.SIGTERM)
            attempt.update(status="signal-sent", signal_sent=True)
            _atomic_write(attempt_path, _json(attempt))
            deadline = time.monotonic() + 5
            while _process_exists(pid) and time.monotonic() < deadline:
                time.sleep(0.1)
            if _process_exists(pid) or _listeners():
                raise WoonError("orphan exit or port release not verified; no further signal")
            _require_unchanged_file(self.config, config_bytes, "companion config")
            _require_unchanged_file(self.settings, settings_bytes, "Runnable settings")
            _require_unchanged_file(self.state, None, "companion state")
            receipt = {
                **plan,
                "receipt_id": receipt_id,
                "applied": True,
                "process_exit_verified": True,
                "port_clear": True,
                "config_preserved": True,
            }
            _atomic_write(attempt_path, _json({**attempt, "status": "verified-awaiting-receipt"}))
            receipt_bytes = _json(receipt)
            _atomic_write(receipt_path, receipt_bytes)
            return receipt
        except Exception as error:
            if receipt_bytes is not None and _current_file_bytes(receipt_path) == receipt_bytes:
                receipt_path.unlink()
            _atomic_write(attempt_path, _json({**attempt, "status": "failed", "applied": False}))
            raise WoonError(f"orphan recovery failed; inspect attempt {receipt_id}") from error

    def _assets(self) -> tuple[str, dict[str, str]]:
        hashes: dict[str, str] = {}
        for name in REQUIRED_ASSETS:
            path = self.settings.parent / name
            _require_vault_local_file(self.vault, path, "Runnable runtime asset")
            hashes[name] = _sha(path.read_bytes())
        manifest = self.service._installed_manifest(RUNNABLE_CODE_BLOCKS_ID)
        if manifest["version"] not in self.plugin_versions:
            raise WoonError("pairing requires the registered plugin release installed and loaded")
        return manifest["version"], hashes

    def pair(
        self,
        cli: Path,
        vault_name: str,
        *,
        apply: bool = False,
        expected_state: str | None = None,
    ) -> dict[str, Any]:
        """Pair through public SecretStorage, preserving existing unequal secrets.

        A failed apply keeps local execution disabled when we still own the files.
        A newly set secret can remain pending; failures never get success receipts.
        """
        status = self.status()
        if not status["ready"]:
            raise WoonError("start the managed companion and verify readiness before pairing")
        plugin_version, assets = self._assets()
        before = self._disk_policy()
        if _sha(before) != status["settings_sha256"]:
            raise WoonError("Runnable settings changed since companion status")
        selected_secret = json.loads(before).get(
            "localRunnerSecretId", "runnable-code-blocks-local-runner-token"
        )
        if selected_secret != "runnable-code-blocks-local-runner-token":
            raise WoonError(
                "pairing requires the default managed secret selection; "
                "preserve the selected secret"
            )
        runtime_config = {
            "vault": str(self.vault),
            "version": plugin_version,
            "assets": assets,
            "config_path": str(self.config),
            "config_sha256": status["config_sha256"],
            "settings_sha256": _sha(before),
            "mode": "read",
        }
        runtime = runtime_pairing(cli, vault_name, runtime_config)
        if runtime.get("remote_enabled") is not False:
            raise WoonError("remote policy must already be disabled in the loaded plugin")
        plan = {
            **status,
            "action": "pair-runnable-companion",
            "runtime": runtime,
            "assets": assets,
            "obsidian_cli": str(cli),
            "vault_name": vault_name,
            "policy_sha256": _sha(_json(self.policy)),
        }
        digest = _sha(_json(plan))
        plan.update(expected_state=digest, applied=False, runtime_pairing_verified=False)
        if not apply:
            return plan
        if expected_state != digest:
            raise WoonError("pairing plan changed or expected-state is missing; replan")
        configuration = json.loads(before)
        desired = {
            **configuration,
            "localExecutionEnabled": True,
            "remoteExecutionEnabled": False,
            "localRunnerEndpoint": status["endpoint"],
        }
        if (
            configuration == desired
            and runtime.get("secret_matches") is True
            and runtime.get("local_enabled") is True
            and runtime.get("endpoint") == status["endpoint"]
        ):
            return {**plan, "applied": True, "changed": False, "runtime_pairing_verified": True}
        receipt_id = self.service._receipt_id()
        backup = (
            self.service._local / "backups" / receipt_id / RUNNABLE_CODE_BLOCKS_ID / "data.json"
        )
        attempt_path = self.root / "attempts" / f"{receipt_id}.json"
        receipt_path = self.service._local / "receipts" / f"{receipt_id}.json"
        staged = _json({**desired, "localExecutionEnabled": False})
        final = _json(desired)
        fallback = _json(
            {**configuration, "localExecutionEnabled": False, "remoteExecutionEnabled": False}
        )
        _atomic_write(backup, before)
        attempt: dict[str, Any] = {
            "action": plan["action"],
            "status": "pending",
            "applied": False,
            "receipt_id": receipt_id,
            "before_sha256": _sha(before),
            "backup": backup.relative_to(self.vault).as_posix(),
            "secret_state": "existing" if runtime.get("secret_present") else "absent",
            "source_requests": 0,
            "image_pulls": 0,
        }
        _atomic_write(attempt_path, _json(attempt))
        written: bytes | None = None
        receipt_bytes: bytes | None = None
        try:
            _require_unchanged_file(self.settings, before, "Runnable settings")
            if self._assets() != (plugin_version, assets):
                raise WoonError("Runnable assets changed since pairing plan")
            written = staged
            _atomic_write(self.settings, staged)
            attempt["secret_state"] = "may-be-paired"
            _atomic_write(attempt_path, _json(attempt))
            paired = runtime_pairing(
                cli,
                vault_name,
                {
                    **runtime_config,
                    "mode": "pair",
                    "settings_sha256": _sha(staged),
                    "baseline": runtime,
                },
            )
            if (
                paired.get("secret_matches") is not True
                or paired.get("local_enabled") is not False
                or paired.get("remote_enabled") is not False
            ):
                raise WoonError("pairing was not verified while local execution was disabled")
            _require_unchanged_file(self.settings, staged, "Runnable settings")
            written = final
            _atomic_write(self.settings, final)
            activated = runtime_pairing(
                cli,
                vault_name,
                {
                    **runtime_config,
                    "mode": "activate",
                    "settings_sha256": _sha(final),
                    "baseline": paired,
                },
            )
            verified = runtime_pairing(
                cli,
                vault_name,
                {
                    **runtime_config,
                    "mode": "read",
                    "settings_sha256": _sha(final),
                    "baseline": activated,
                },
            )
            if (
                verified.get("secret_matches") is not True
                or verified.get("local_enabled") is not True
                or verified.get("remote_enabled") is not False
                or verified.get("endpoint") != status["endpoint"]
            ):
                raise WoonError("runtime pairing could not be verified by a separate reread")
            _require_unchanged_file(self.settings, final, "Runnable settings")
            after = self.status()
            if (
                not after["ready"]
                or after["managed_state_sha256"] != status["managed_state_sha256"]
                or after["config_sha256"] != status["config_sha256"]
            ):
                raise WoonError("companion ownership changed during pairing")
            receipt = {
                **plan,
                "receipt_id": receipt_id,
                "applied": True,
                "changed": True,
                "runtime_pairing_verified": True,
                "runtime": verified,
                "before_sha256": _sha(before),
                "settings_sha256": _sha(final),
                "backup": attempt["backup"],
                "secret_created": not runtime["secret_present"],
                "verification": "disk-reread; separate-runtime-reread; authenticated-capabilities",
                "ui_verified": False,
                "sample_executions": 0,
            }
            # Pending attempts are not success proof; the separate receipt is committed last.
            _atomic_write(attempt_path, _json({**attempt, "status": "verified-awaiting-receipt"}))
            receipt_bytes = _json(receipt)
            _atomic_write(receipt_path, receipt_bytes)
            return receipt
        except Exception as error:
            if receipt_bytes is not None and _current_file_bytes(receipt_path) == receipt_bytes:
                receipt_path.unlink()
            current_settings = _current_file_bytes(self.settings)
            owned = written is not None and current_settings in {written, before}
            attempt.update(
                status="failed", local_disk_disabled=False, runtime_disable_verified=False
            )
            if owned and current_settings is not None:
                _restore_file_if_owned(
                    self.settings,
                    expected_current=current_settings,
                    previous=fallback,
                    label="Runnable settings",
                    cause=error,
                )
                attempt["local_disk_disabled"] = True
                try:
                    disabled = runtime_pairing(
                        cli,
                        vault_name,
                        {
                            **runtime_config,
                            "mode": "disable",
                            "settings_sha256": _sha(fallback),
                            "baseline": runtime,
                        },
                    )
                    attempt["runtime_disable_verified"] = (
                        disabled.get("local_enabled") is False
                        and disabled.get("remote_enabled") is False
                    )
                except WoonError:
                    pass
            attempt["concurrent_settings_preserved"] = not owned
            _atomic_write(attempt_path, _json(attempt))
            raise WoonError(
                f"pairing failed; inspect attempt {receipt_id}; secret may remain pending; "
                "no success receipt"
            ) from error
