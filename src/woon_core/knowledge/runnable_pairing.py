"""Registered Obsidian CLI pairing; token bytes never enter command arguments."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from woon_core.errors import WoonError
from woon_core.knowledge.local_settings import _runtime_cli
from woon_core.knowledge.obsidian_plugins import (
    DATA_FILE_NAME,
    MANTA_ID,
    RUNNABLE_CODE_BLOCKS_ID,
)

# The runner service (manta-code-blocks/local-runner) owns and writes this file for both
# plugins; Manta only reads the token from SecretStorage, so it defines no path of its own.
COMPANION_CONFIG_PATH = ".config/runnable-code-blocks/local-runner.json"


@dataclass(frozen=True)
class RunnableTarget:
    """The plugin that executes local code blocks and where it keeps its pairing state.

    ``settings_scope`` is the key holding the run settings inside ``data.json`` and the
    plugin's runtime ``settings`` object; ``None`` means the settings sit at the root.
    """

    plugin_id: str
    settings_scope: str | None
    secret_id: str
    config_path: str = COMPANION_CONFIG_PATH

    def read(self, configuration: dict[str, Any]) -> dict[str, Any]:
        if self.settings_scope is None:
            return configuration
        scoped = configuration.get(self.settings_scope, {})
        if not isinstance(scoped, dict):
            raise WoonError(f"{self.plugin_id} settings {self.settings_scope!r} must be an object")
        return scoped

    def merge(self, configuration: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
        """Return ``configuration`` with ``patch`` applied inside the run settings scope."""
        if self.settings_scope is None:
            return {**configuration, **patch}
        return {**configuration, self.settings_scope: {**self.read(configuration), **patch}}

    def script_config(self) -> dict[str, Any]:
        return {
            "plugin_id": self.plugin_id,
            "scope": self.settings_scope,
            "secret_id": self.secret_id,
            "config_path": self.config_path,
            "settings_file": DATA_FILE_NAME,
        }


MANTA_TARGET = RunnableTarget(MANTA_ID, "run", "manta-run-local-runner-token")
LEGACY_RUNNABLE_TARGET = RunnableTarget(
    RUNNABLE_CODE_BLOCKS_ID, None, "runnable-code-blocks-local-runner-token"
)


def resolve_runnable_target(plugins_directory: Path) -> RunnableTarget:
    """Manta wins when installed; the standalone plugin is used only while it is alone."""

    def installed(plugin_id: str) -> bool:
        return (plugins_directory / plugin_id / "manifest.json").is_file()

    if not installed(MANTA_ID) and installed(RUNNABLE_CODE_BLOCKS_ID):
        return LEGACY_RUNNABLE_TARGET
    return MANTA_TARGET


# Only the companion's standard config is read. SecretStorage has no public delete
# API: a late failure leaves a matching secret pending and disables local execution.
_PAIRING_SCRIPT = r"""
(async () => {
  const c = CONFIG;
  const fail = reason => ({status: "error", reason});
  try {
    if (app.vault.adapter.getBasePath() !== c.vault) return fail("wrong-vault");
    const t = c.target;
    const scoped = value => t.scope ? value?.[t.scope] : value;
    const p = app.plugins.getPlugin(t.plugin_id);
    const epoch = performance.timeOrigin;
    if (!p || p.manifest.version !== c.version || typeof p.loadSettings !== "function")
      return fail("unavailable-plugin");
    if (c.baseline && epoch !== c.baseline.window_epoch) return fail("window-changed");
    const fs = require("node:fs");
    const path = require("node:path");
    const hash = value => require("node:crypto").createHash("sha256").update(value).digest("hex");
    const regular = name => {
      let current = name;
      while (current !== c.vault && current !== path.dirname(current)) {
        if (fs.lstatSync(current).isSymbolicLink()) throw new Error("symlink");
        current = path.dirname(current);
      }
      if (!fs.statSync(name).isFile()) throw new Error("not-file");
      return fs.readFileSync(name);
    };
    const settingsPath = path.join(c.vault, ".obsidian/plugins", t.plugin_id, t.settings_file);
    const disk = regular(settingsPath);
    const diskSettings = scoped(JSON.parse(disk)) ?? {};
    if (hash(disk) !== c.settings_sha256 || diskSettings.remoteExecutionEnabled !== false)
      return fail("disk-changed");
    // Re-read after loadSettings(): a plugin may replace its settings object on reload.
    const live = () => scoped(p.settings);
    if (!live() || typeof live() !== "object") return fail("settings-scope-missing");
    for (const [name, digest] of Object.entries(c.assets)) {
      if (hash(regular(path.join(path.dirname(settingsPath), name))) !== digest)
        return fail("assets-changed");
    }
    if (c.mode === "disable") {
      live().localExecutionEnabled = false;
      live().remoteExecutionEnabled = false;
      return {status: "ok", local_enabled: false, remote_enabled: false, window_epoch: epoch};
    }
    const standardConfig = path.join(require("node:os").homedir(), t.config_path);
    if (standardConfig !== c.config_path) return fail("wrong-config-path");
    const configBytes = regular(standardConfig);
    const st = fs.statSync(standardConfig);
    if ((st.mode & 0o077) !== 0 || st.uid !== process.getuid() ||
        hash(configBytes) !== c.config_sha256)
      return fail("config-changed");
    const config = JSON.parse(configBytes);
    if (config.port !== 17171 || typeof config.token !== "string" ||
        !/^[A-Za-z0-9_-]{32,256}$/.test(config.token)) return fail("invalid-config");
    const storage = app.secretStorage;
    if (!storage || typeof storage.getSecret !== "function" ||
        typeof storage.setSecret !== "function")
      return fail("secret-api-unavailable");
    const id = t.secret_id;
    if ((diskSettings.localRunnerSecretId ?? id) !== id ||
        (live().localRunnerSecretId ?? id) !== id) return fail("selected-secret-mismatch");
    const snapshot = () => {
      const existing = storage.getSecret(id);
      if (existing && existing !== config.token) throw new Error("secret-conflict");
      return {status: "ok", window_epoch: epoch, version: p.manifest.version,
        local_enabled: live().localExecutionEnabled,
        remote_enabled: live().remoteExecutionEnabled,
        endpoint: live().localRunnerEndpoint,
        secret_present: !!existing, secret_matches: existing === config.token};
    };
    const before = snapshot();
    if (before.remote_enabled !== false) return fail("remote-policy-not-loaded");
    if (c.baseline) {
      for (const key of Object.keys(before)) {
        if (before[key] !== c.baseline[key]) return fail("runtime-changed");
      }
    }
    if (c.mode === "pair") {
      live().localExecutionEnabled = false;
      await p.loadSettings();
      const staged = scoped(p.settings);
      if (staged?.localExecutionEnabled !== false || staged?.remoteExecutionEnabled !== false)
        return fail("staged-policy-not-loaded");
      const current = storage.getSecret(id);
      if (current && current !== config.token) return fail("secret-conflict");
      if (!current) storage.setSecret(id, config.token);
    } else if (c.mode === "activate") {
      if (!before.secret_matches) return fail("secret-not-paired");
      await p.loadSettings();
    } else if (c.mode !== "read") return fail("invalid-mode");
    const result = snapshot();
    if (result.remote_enabled !== false) return fail("remote-policy-changed");
    if (c.mode === "activate" &&
        (result.local_enabled !== true || result.endpoint !== "http://127.0.0.1:17171"))
      return fail("local-policy-not-loaded");
    return result;
  } catch (_) {
    return fail("pairing-operation-failed");
  }
})().then(value => JSON.stringify(value))
"""


def runtime_pairing(cli: Path, vault_name: str, configuration: dict[str, Any]) -> dict[str, Any]:
    """Use only on the already-running owner-verified Vault; do not start Obsidian."""
    if not cli.is_absolute() or not cli.is_file() or not vault_name.strip():
        raise WoonError("pairing requires an existing Obsidian CLI and exact running Vault")
    if not isinstance(configuration.get("target"), dict):
        raise WoonError("pairing requires an explicit runnable target")
    code = _PAIRING_SCRIPT.replace("CONFIG", json.dumps(configuration), 1)
    raw = _runtime_cli(cli, vault_name, "eval", f"code={code}").removeprefix("=> ")
    try:
        value = json.loads(raw)
        if isinstance(value, str):
            value = json.loads(value)
    except ValueError as error:
        raise WoonError("pairing CLI did not return a completed JSON result") from error
    if not isinstance(value, dict) or value.get("status") != "ok":
        raise WoonError("pairing runtime check failed; existing secrets are preserved")
    return value
