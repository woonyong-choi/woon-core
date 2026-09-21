from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from woon_core.knowledge.runnable_companion import _json, _sha
from woon_core.knowledge.runnable_pairing import (
    _PAIRING_SCRIPT,
    LEGACY_RUNNABLE_TARGET,
    MANTA_TARGET,
    resolve_runnable_target,
)


def test_manta_is_preferred_and_the_standalone_plugin_is_only_a_fallback(tmp_path):
    plugins = tmp_path / "plugins"

    def install(plugin_id):
        (plugins / plugin_id).mkdir(parents=True)
        (plugins / plugin_id / "manifest.json").write_text("{}")

    assert resolve_runnable_target(plugins) == MANTA_TARGET
    install("runnable-code-blocks")
    assert resolve_runnable_target(plugins) == LEGACY_RUNNABLE_TARGET
    install("manta")
    assert resolve_runnable_target(plugins) == MANTA_TARGET
    assert MANTA_TARGET.settings_scope == "run"
    assert MANTA_TARGET.secret_id == "manta-run-local-runner-token"


@pytest.mark.parametrize("target", [MANTA_TARGET, LEGACY_RUNNABLE_TARGET], ids=["manta", "legacy"])
def test_public_secret_api_pairing_preserves_conflicts_and_requires_separate_reload(
    tmp_path, target
):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is needed for the isolated Obsidian adapter harness")
    vault = tmp_path / "vault"
    plugin = vault / ".obsidian/plugins" / target.plugin_id
    plugin.mkdir(parents=True)
    config = tmp_path / "home/.config/runnable-code-blocks/local-runner.json"
    config.parent.mkdir(parents=True)
    token = "unit-test-only-token-" + "x" * 32
    config.write_bytes(_json({"port": 17171, "token": token}))
    config.chmod(0o600)
    settings = plugin / "data.json"
    settings.write_bytes(
        _json(
            target.merge(
                {"locale": "auto"},
                {
                    "remoteExecutionEnabled": False,
                    "localExecutionEnabled": True,
                    "localRunnerEndpoint": "http://127.0.0.1:17171",
                },
            )
        )
    )
    assets = {}
    for name in ("main.js", "styles.css", "manifest.json"):
        (plugin / name).write_bytes(b"test-only")
        assets[name] = _sha(b"test-only")
    spec = {
        "vault": str(vault),
        "version": "0.7.2",
        "assets": assets,
        "config_path": str(config),
        "config_sha256": _sha(config.read_bytes()),
        "settings_sha256": _sha(settings.read_bytes()),
        "target": target.script_config(),
        "mode": "read",
    }
    assert token not in _PAIRING_SCRIPT.replace("CONFIG", json.dumps(spec), 1)
    harness = r"""
const fs = require('node:fs');
const path = require('node:path');
const hash = data => require('node:crypto').createHash('sha256').update(data).digest('hex');
const spec = SPEC;
const template = TEMPLATE;
const scope = spec.target.scope;
const live = () => scope ? p.settings[scope] : p.settings;
const settingsPath = path.join(spec.vault, '.obsidian/plugins', spec.target.plugin_id, 'data.json');
let secret = null, sets = 0, loads = 0;
const p = {manifest: {version: '0.7.2'}, settings: JSON.parse(fs.readFileSync(settingsPath)),
  loadSettings: async () => { loads++; p.settings = JSON.parse(fs.readFileSync(settingsPath)); }};
const app = {
  vault: {adapter: {getBasePath: () => spec.vault}},
  plugins: {getPlugin: id => id === spec.target.plugin_id ? p : undefined},
  secretStorage: {getSecret: () => secret, setSecret: (id, value) => {
    if (id !== spec.target.secret_id) throw new Error('wrong-secret-id');
    sets++; secret = value;
  }}
};
const mockedRequire = name => name === 'node:os'
  ? {homedir: () => path.dirname(path.dirname(path.dirname(spec.config_path)))} : require(name);
const execute = async config => JSON.parse(await new Function(
  'app','require','performance','process',
  'return (' + template.replace('CONFIG', JSON.stringify(config)) + ')')(app, mockedRequire,
    {timeOrigin: 100}, process));
const writePolicy = enabled => {
  const run = {remoteExecutionEnabled: false, localExecutionEnabled: enabled,
    localRunnerEndpoint: 'http://127.0.0.1:17171'};
  fs.writeFileSync(settingsPath, JSON.stringify(scope ? {locale: 'auto', [scope]: run} : run));
  return hash(fs.readFileSync(settingsPath));
};
(async () => {
  const before = await execute(spec);
  const stagedHash = writePolicy(false);
  const paired = await execute({...spec, mode: 'pair',
    settings_sha256: stagedHash, baseline: before});
  const finalHash = writePolicy(true);
  const active = await execute({...spec, mode: 'activate',
    settings_sha256: finalHash, baseline: paired});
  const verified = await execute({...spec, settings_sha256: finalHash, baseline: active});
  const token = secret;
  secret = 'unrelated-existing-secret';
  const conflict = await execute({...spec, mode: 'pair', settings_sha256: finalHash});
  const preserved = secret === 'unrelated-existing-secret';
  secret = token;
  const wrongWindow = await execute({...spec, settings_sha256: finalHash,
    baseline: {...active, window_epoch: 999}});
  p.manifest.version = '0.7.3';
  const wrongVersion = await execute({...spec, mode: 'pair', settings_sha256: finalHash});
  const nextVersion = await execute({...spec, version: '0.7.3', settings_sha256: finalHash});
  live().localRunnerSecretId = 'user-selected-secret';
  const wrongSecret = await execute({...spec, version: '0.7.3', settings_sha256: finalHash,
    mode: 'pair'});
  process.stdout.write(JSON.stringify({before, paired, verified, conflict, preserved, wrongWindow,
    wrongVersion, nextVersion, wrongSecret, sets, loads}));
})().catch(() => {
  process.stderr.write('isolated adapter harness failed'); process.exitCode = 1;
});
"""
    harness = harness.replace("SPEC", json.dumps(spec), 1).replace(
        "TEMPLATE",
        json.dumps(_PAIRING_SCRIPT),
        1,
    )
    completed = subprocess.run([node], input=harness, capture_output=True, text=True, timeout=10)
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result["before"]["secret_present"] is False
    assert result["paired"]["secret_matches"] is True
    assert result["paired"]["local_enabled"] is False
    assert result["verified"]["local_enabled"] is True
    assert result["verified"]["remote_enabled"] is False
    assert result["sets"] == 1 and result["loads"] == 2
    assert result["conflict"]["status"] == result["wrongWindow"]["status"] == "error"
    assert result["wrongVersion"]["reason"] == "unavailable-plugin"
    assert result["nextVersion"]["version"] == "0.7.3"
    assert result["nextVersion"]["secret_matches"] is True
    assert result["wrongSecret"]["reason"] == "selected-secret-mismatch"
    assert result["preserved"] is True
    assert token not in completed.stdout
