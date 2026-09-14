from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

from woon_core.errors import WoonError
from woon_core.knowledge import obsidian_plugins
from woon_core.knowledge.obsidian_plugins import (
    FULL_CALENDAR_REMASTERED_ID,
    LEGACY_CONTEXT_CALENDAR_ID,
    LEGACY_CONTEXT_GRAPH_ID,
    LEGACY_SIMPLE_CALENDAR_ID,
    LINK_CALENDAR_ID,
    LINK_CALENDAR_MANUAL_ATTESTATION_CHECKS,
    LINK_CALENDAR_VERSION,
    LINKED_GRAPH_ID,
    LINKED_GRAPH_VERSION,
    NOTION_BASES_ID,
    PRISMA_CALENDAR_ID,
    RUNNABLE_CODE_BLOCKS_ID,
    RUNNABLE_CODE_BLOCKS_VERSION,
    ObsidianPluginService,
)


def _release(
    plugin_id: str, version: str, base_url: str
) -> tuple[dict[str, object], dict[str, bytes]]:
    assets = {
        "main.js": b"module.exports = {};\n",
        "manifest.json": json.dumps(
            {"id": plugin_id, "name": plugin_id, "version": version, "minAppVersion": "1.4.0"}
        ).encode(),
        "styles.css": b".mindmap { color: inherit; }\n",
    }
    return (
        {
            "tag_name": version,
            "html_url": f"{base_url}/release",
            "assets": [
                {
                    "name": name,
                    "browser_download_url": f"https://github.com/example/{plugin_id}/{name}",
                    "digest": "sha256:" + hashlib.sha256(content).hexdigest(),
                }
                for name, content in assets.items()
            ],
        },
        assets,
    )


def _vault(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    (vault / ".obsidian" / "plugins").mkdir(parents=True)
    (vault / ".obsidian" / "community-plugins.json").write_text('["homepage"]\n', encoding="utf-8")
    return vault


def test_retire_apple_source_preserves_google_data_and_is_idempotent(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    plugin = vault / ".obsidian/plugins/link-calendar"
    plugin.mkdir(parents=True)
    (plugin / "manifest.json").write_text(json.dumps({"id": "link-calendar", "version": "9.0"}))
    settings = plugin / "data.json"
    configuration = {
        "sourceProfiles": [{"id": "woon-apple-calendar"}, {"id": "personal", "editable": True}],
        "googleCalendar": {
            "sourceProfileIds": ["personal", "woon-apple-calendar"],
            "calendarId": "existing-calendar",
            "refreshToken": "private-secret",
            "records": {"old-event": {"remoteId": "existing-google-event"}},
        },
        "customSetting": {"preserve": True},
    }
    settings.write_text(json.dumps(configuration))
    before = settings.read_bytes()
    document = vault / "existing-apple-event.md"
    document.write_text("Existing event remains.\n")
    service = ObsidianPluginService(vault)
    preview = service.retire_apple_calendar_source()
    assert preview["changed"] is True and preview["applied"] is False
    assert settings.read_bytes() == before
    assert not (vault / ".local").exists()
    result = service.retire_apple_calendar_source(
        apply=True, expected_settings_sha256=preview["before_sha256"]
    )
    expected = json.loads(before)
    expected["sourceProfiles"] = [configuration["sourceProfiles"][1]]
    expected["googleCalendar"]["sourceProfileIds"] = ["personal"]
    assert json.loads(settings.read_bytes()) == expected
    assert (vault / result["backup"]).read_bytes() == before
    assert document.read_text() == "Existing event remains.\n"
    assert "private-secret" not in json.dumps(result)
    assert result["remote_writes"] == 0 and result["ui_verified"] is False
    current = settings.read_bytes()
    again = service.retire_apple_calendar_source(
        apply=True, expected_settings_sha256=hashlib.sha256(current).hexdigest()
    )
    assert again["changed"] is False and settings.read_bytes() == current


@pytest.mark.parametrize("remote", [None, True, False])
def test_disable_runnable_remote_preserves_legacy_settings_and_secrets(
    tmp_path: Path,
    remote: bool | None,
) -> None:
    vault = _vault(tmp_path)
    plugin = vault / ".obsidian/plugins" / RUNNABLE_CODE_BLOCKS_ID
    plugin.mkdir()
    _, assets = _release(RUNNABLE_CODE_BLOCKS_ID, "0.7.1", "https://example.invalid")
    for name, content in assets.items():
        (plugin / name).write_bytes(content)
    configuration = {
        "kotlinCompilerPath": "/old/compiler",
        "javaPath": "/old/java",
        "localExecutionEnabled": False,
        "localRunnerEndpoint": "http://127.0.0.1:17171",
        "legacyToken": "do-not-print-this-secret",
        "custom": {"unchanged": [1, 2]},
    }
    if remote is not None:
        configuration["remoteExecutionEnabled"] = remote
    settings = plugin / "data.json"
    settings.write_text(json.dumps(configuration) + "\n\n")
    before = settings.read_bytes()
    enabled = (vault / ".obsidian/community-plugins.json").read_bytes()
    service = ObsidianPluginService(vault)
    preview = service.disable_runnable_remote_execution()
    assert settings.read_bytes() == before
    assert not (vault / ".local").exists()
    assert preview["changed"] is (remote is not False)
    applied = service.disable_runnable_remote_execution(
        apply=True, expected_settings_sha256=preview["before_sha256"]
    )
    assert json.loads(settings.read_bytes()) == {**configuration, "remoteExecutionEnabled": False}
    assert (vault / applied["backup"]).read_bytes() == before
    assert applied["sha256"] == hashlib.sha256(settings.read_bytes()).hexdigest()
    assert applied["disk_policy_verified"] is True
    assert applied["runtime_policy_verified"] is False
    assert applied["runner_invocations"] == applied["external_transmissions"] == 0
    assert "do-not-print-this-secret" not in json.dumps(applied)
    assert (vault / ".obsidian/community-plugins.json").read_bytes() == enabled
    if remote is False:
        assert settings.read_bytes() == before
    current = settings.read_bytes()
    repeated = service.disable_runnable_remote_execution(
        apply=True, expected_settings_sha256=applied["sha256"]
    )
    assert repeated["changed"] is False and settings.read_bytes() == current
    for path in (vault / ".local/woon-knowledge/obsidian-plugins").rglob("*.json"):
        assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    "action",
    [
        "retire_apple_calendar_source",
        "configure_google_two_way_source",
        "disable_runnable_remote_execution",
    ],
)
def test_calendar_source_change_rejects_stale_preview_and_rolls_back_receipt_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
) -> None:
    vault = _vault(tmp_path)
    plugin_id = (
        RUNNABLE_CODE_BLOCKS_ID if action.startswith("disable_runnable") else LINK_CALENDAR_ID
    )
    plugin = vault / ".obsidian/plugins" / plugin_id
    plugin.mkdir(parents=True)
    _, assets = _release(plugin_id, "9.0", "https://example.invalid")
    for name, content in assets.items():
        (plugin / name).write_bytes(content)
    settings = plugin / "data.json"
    settings.write_text('{"sourceProfiles":[{"id":"woon-apple-calendar"}],"googleCalendar":{}}')
    service = ObsidianPluginService(vault)
    operation = getattr(service, action)
    preview = operation()
    settings.write_text(settings.read_text() + "\n")
    before = settings.read_bytes()
    with pytest.raises(WoonError, match="replan"):
        operation(apply=True, expected_settings_sha256=preview["before_sha256"])
    assert settings.read_bytes() == before
    atomic_write = obsidian_plugins._atomic_write

    def fail_receipt(path: Path, content: bytes) -> None:
        if path.parent.name == "receipts":
            raise OSError("receipt failed")
        atomic_write(path, content)

    monkeypatch.setattr(obsidian_plugins, "_atomic_write", fail_receipt)
    with pytest.raises(OSError, match="receipt failed"):
        operation(apply=True, expected_settings_sha256=hashlib.sha256(before).hexdigest())
    assert settings.read_bytes() == before
    assert not list((vault / ".local/woon-knowledge/obsidian-plugins/receipts").glob("*.json"))


def test_runnable_policy_rollback_preserves_concurrent_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = _vault(tmp_path)
    plugin = vault / ".obsidian/plugins" / RUNNABLE_CODE_BLOCKS_ID
    plugin.mkdir()
    _, assets = _release(RUNNABLE_CODE_BLOCKS_ID, "0.7.1", "https://example.invalid")
    for name, content in assets.items():
        (plugin / name).write_bytes(content)
    settings = plugin / "data.json"
    settings.write_text('{"remoteExecutionEnabled":true,"custom":"original"}')
    before = settings.read_bytes()
    service = ObsidianPluginService(vault)
    preview = service.disable_runnable_remote_execution()
    concurrent = b'{"remoteExecutionEnabled":false,"custom":"user-edit"}'
    atomic_write = obsidian_plugins._atomic_write

    def interrupt_receipt(path: Path, content: bytes) -> None:
        if path.parent.name == "receipts":
            atomic_write(path, content)
            settings.write_bytes(concurrent)
            raise OSError("receipt failed")
        atomic_write(path, content)

    monkeypatch.setattr(obsidian_plugins, "_atomic_write", interrupt_receipt)
    with pytest.raises(WoonError, match="rollback refused"):
        service.disable_runnable_remote_execution(
            apply=True, expected_settings_sha256=preview["before_sha256"]
        )
    assert settings.read_bytes() == concurrent
    backups = list((vault / ".local/woon-knowledge/obsidian-plugins/backups").rglob("data.json"))
    assert len(backups) == 1 and backups[0].read_bytes() == before
    assert not list((vault / ".local/woon-knowledge/obsidian-plugins/receipts").glob("*.json"))


@pytest.mark.parametrize("collection", ["sourceProfiles", "profiles"])
def test_google_two_way_source_preserves_account_mappings_and_documents(
    tmp_path: Path,
    collection: str,
) -> None:
    vault = _vault(tmp_path)
    plugin = vault / ".obsidian/plugins/link-calendar"
    plugin.mkdir(parents=True)
    (plugin / "manifest.json").write_text(json.dumps({"id": "link-calendar", "version": "9.0"}))
    settings = plugin / "data.json"
    other = {"id": "user", "folder": "appointments", "editable": False}
    google = {
        "enabled": True,
        "calendar": {"id": "existing", "timeZone": "Asia/Seoul"},
        "sourceProfileIds": ["user"],
        "refreshToken": "private-secret",
        "records": [{"eventId": "keep", "etag": "remote-version", "localKey": "old-event"}],
    }
    original = {collection: [other], "googleCalendar": google, "userSetting": {"preserve": True}}
    settings.write_text(json.dumps(original))
    before = settings.read_bytes()
    document = vault / "inbox/calendar/google/existing.md"
    document.parent.mkdir(parents=True)
    document.write_bytes(b"User-owned existing note.\n")
    service = ObsidianPluginService(vault)
    preview = service.configure_google_two_way_source()
    assert preview["changed"] is True and preview["applied"] is False
    assert settings.read_bytes() == before and not (vault / ".local").exists()
    applied = service.configure_google_two_way_source(
        apply=True, expected_settings_sha256=preview["before_sha256"]
    )
    after = json.loads(settings.read_bytes())
    assert after[collection][0] == other
    assert len(after[collection]) == 2
    profile = after[collection][1]
    assert profile["id"] == "woon-google-calendar" and profile["folder"] == "inbox/calendar/google"
    assert profile["editable"] is True and profile["enabled"] is True
    assert profile["properties"] == {
        "allDay": "allDay",
        "category": "category",
        "end": "end",
        "endTime": "endTime",
        "start": "date",
        "startTime": "startTime",
        "title": "title",
    }
    assert after["googleCalendar"] == {
        **google,
        "sourceProfileIds": ["user", "woon-google-calendar"],
        "incomingProfileId": "woon-google-calendar",
    }
    assert after["userSetting"] == original["userSetting"]
    assert set(after) == set(original)
    assert document.read_bytes() == b"User-owned existing note.\n"
    assert list(document.parent.iterdir()) == [document]
    assert (vault / applied["backup"]).read_bytes() == before
    assert "private-secret" not in json.dumps(applied)
    assert applied["remote_writes"] == 0 and applied["sync_verified"] is False
    current = settings.read_bytes()
    receipts = list((vault / ".local/woon-knowledge/obsidian-plugins/receipts").glob("*.json"))
    repeated = service.configure_google_two_way_source(
        apply=True, expected_settings_sha256=hashlib.sha256(current).hexdigest()
    )
    assert repeated["changed"] is False and settings.read_bytes() == current
    assert (
        list((vault / ".local/woon-knowledge/obsidian-plugins/receipts").glob("*.json")) == receipts
    )


@pytest.mark.parametrize("conflict", ["existing-id", "overlap", "incoming", "symlink"])
def test_google_two_way_source_preserves_conflicting_configuration(
    tmp_path: Path,
    conflict: str,
) -> None:
    vault = _vault(tmp_path)
    plugin = vault / ".obsidian/plugins/link-calendar"
    plugin.mkdir(parents=True)
    (plugin / "manifest.json").write_text(json.dumps({"id": "link-calendar", "version": "9.0"}))
    settings = plugin / "data.json"
    configuration = {"sourceProfiles": [], "googleCalendar": {}}
    if conflict == "existing-id":
        configuration["sourceProfiles"] = [{"id": "woon-google-calendar", "folder": "user-notes"}]
    elif conflict == "overlap":
        configuration["sourceProfiles"] = [{"id": "user", "folder": "inbox", "recursive": True}]
    elif conflict == "incoming":
        configuration["googleCalendar"] = {"incomingProfileId": "user-destination"}
    else:
        outside = tmp_path / "outside"
        outside.mkdir()
        (vault / "inbox").symlink_to(outside, target_is_directory=True)
    settings.write_text(json.dumps(configuration))
    before = settings.read_bytes()
    service = ObsidianPluginService(vault)
    with pytest.raises(WoonError, match="Google"):
        service.configure_google_two_way_source()
    assert settings.read_bytes() == before
    assert not (vault / ".local").exists()


def test_install_verifies_release_manifest_assets_and_enabled_config(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    releases: dict[str, bytes] = {}
    for plugin_id, version, repository in (
        (LINK_CALENDAR_ID, "3.3.0", "woonyong-choi/manta-calendar"),
        (LINKED_GRAPH_ID, "0.5.6", "woonyong-choi/manta-graph"),
        (RUNNABLE_CODE_BLOCKS_ID, "0.2.4", "woonyong-choi/manta-code-blocks"),
        ("light-mindmap", "1.5.0", "ninglg/light-mindmap"),
        ("markdown-mindmap", "1.4.2", "kikocastro/markdown-mindmap"),
        (PRISMA_CALENDAR_ID, "2.22.0", "Real1tyy/Prisma-Calendar"),
        (NOTION_BASES_ID, "1.12.0", "bgarciamoura/obsidian-notion-bases-plugin"),
    ):
        release, assets = _release(plugin_id, version, f"https://github.com/{repository}")
        releases[f"https://api.github.com/repos/{repository}/releases/latest"] = json.dumps(
            release
        ).encode()
        releases.update(
            {
                f"https://github.com/example/{plugin_id}/{name}": content
                for name, content in assets.items()
            }
        )

    receipt = ObsidianPluginService(vault, download=releases.__getitem__).install(
        [
            LINK_CALENDAR_ID,
            LINKED_GRAPH_ID,
            RUNNABLE_CODE_BLOCKS_ID,
            "light-mindmap",
            "markdown-mindmap",
            PRISMA_CALENDAR_ID,
            NOTION_BASES_ID,
        ]
    )

    assert [item["id"] for item in receipt["plugins"]] == [
        LINK_CALENDAR_ID,
        LINKED_GRAPH_ID,
        RUNNABLE_CODE_BLOCKS_ID,
        "light-mindmap",
        "markdown-mindmap",
        PRISMA_CALENDAR_ID,
        NOTION_BASES_ID,
    ]
    status = ObsidianPluginService(vault, download=releases.__getitem__).status()
    assert {item["id"] for item in status["plugins"]} == {
        LINK_CALENDAR_ID,
        LINKED_GRAPH_ID,
        RUNNABLE_CODE_BLOCKS_ID,
        "light-mindmap",
        "markdown-mindmap",
        PRISMA_CALENDAR_ID,
        NOTION_BASES_ID,
    }
    assert all(item["enabled_in_config"] for item in status["plugins"])
    assert list(
        (vault / ".local" / "woon-knowledge" / "obsidian-plugins" / "receipts").glob("*.json")
    )


def test_official_install_preserves_existing_plugin_settings(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    destination = vault / ".obsidian/plugins" / LINKED_GRAPH_ID
    destination.mkdir()
    for name, content in {
        "main.js": b"old runtime\n",
        "manifest.json": json.dumps({"id": LINKED_GRAPH_ID, "version": "0.5.5"}).encode(),
        "styles.css": b"old styles\n",
        "data.json": b'{"defaultGraphId":"interview"}\n',
    }.items():
        (destination / name).write_bytes(content)
    release, assets = _release(
        LINKED_GRAPH_ID, "0.5.12", "https://github.com/woonyong-choi/manta-graph"
    )
    release_endpoint = "https://api.github.com/repos/woonyong-choi/manta-graph/releases/latest"
    downloads = {
        release_endpoint: json.dumps(release).encode(),
        **{
            f"https://github.com/example/{LINKED_GRAPH_ID}/{name}": content
            for name, content in assets.items()
        },
    }

    receipt = ObsidianPluginService(vault, download=downloads.__getitem__).install(
        [LINKED_GRAPH_ID]
    )

    assert receipt["plugins"][0]["preserved_settings"] == ["data.json"]
    assert (destination / "data.json").read_bytes() == b'{"defaultGraphId":"interview"}\n'


def test_recover_settings_restores_only_non_runtime_files_from_exact_backup(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    destination = vault / ".obsidian/plugins" / LINKED_GRAPH_ID
    destination.mkdir()
    for name in ("main.js", "styles.css"):
        (destination / name).write_text("current runtime\n", encoding="utf-8")
    (destination / "manifest.json").write_text(
        json.dumps({"id": LINKED_GRAPH_ID, "name": "Linked Graph", "version": "0.5.12"}),
        encoding="utf-8",
    )
    source_receipt_id = "obsidian-plugin-20260824T000000Z-example"
    receipt_root = vault / ".local/woon-knowledge/obsidian-plugins/receipts"
    receipt_root.mkdir(parents=True)
    (receipt_root / f"{source_receipt_id}.json").write_text(
        json.dumps(
            {
                "receipt_id": source_receipt_id,
                "action": "install",
                "plugins": [{"id": LINKED_GRAPH_ID}],
            }
        ),
        encoding="utf-8",
    )
    backup = (
        vault
        / ".local/woon-knowledge/obsidian-plugins/backups"
        / source_receipt_id
        / LINKED_GRAPH_ID
    )
    backup.mkdir(parents=True)
    (backup / "main.js").write_text("old runtime\n", encoding="utf-8")
    (backup / "data.json").write_text('{"defaultGraphId":"interview"}\n', encoding="utf-8")

    receipt = ObsidianPluginService(vault).recover_settings_from_backup(
        LINKED_GRAPH_ID, source_receipt_id
    )

    assert receipt["action"] == "recover-settings"
    assert (destination / "main.js").read_text(encoding="utf-8") == "current runtime\n"
    assert (destination / "data.json").read_text(encoding="utf-8") == (
        '{"defaultGraphId":"interview"}\n'
    )


def test_remove_detected_mindmaps_preserves_backup_and_unrelated_plugins(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    plugins = vault / ".obsidian" / "plugins"
    old_map = plugins / "old-mindmap"
    old_map.mkdir()
    (old_map / "manifest.json").write_text(
        json.dumps({"id": "old-mindmap", "name": "Old Mindmap", "version": "0.1.0"}),
        encoding="utf-8",
    )
    unrelated = plugins / "homepage"
    unrelated.mkdir()
    (unrelated / "manifest.json").write_text(
        json.dumps({"id": "homepage", "name": "Homepage", "version": "1.0.0"}),
        encoding="utf-8",
    )
    (vault / ".obsidian" / "community-plugins.json").write_text(
        '["old-mindmap", "homepage"]\n', encoding="utf-8"
    )

    receipt = ObsidianPluginService(vault).remove_detected_mindmaps()

    assert receipt["removed"] == ["old-mindmap"]
    assert not old_map.exists()
    assert unrelated.is_dir()
    assert "old-mindmap" not in json.loads(
        (vault / ".obsidian" / "community-plugins.json").read_text()
    )
    assert list(
        (vault / ".local" / "woon-knowledge" / "obsidian-plugins" / "backups").rglob("old-mindmap")
    )


def test_install_restores_existing_plugin_if_stage_replace_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = _vault(tmp_path)
    plugins = vault / ".obsidian" / "plugins"
    existing = plugins / "light-mindmap"
    existing.mkdir()
    (existing / "manifest.json").write_text(
        json.dumps({"id": "light-mindmap", "name": "Old Light Mindmap", "version": "0.1.0"}),
        encoding="utf-8",
    )
    release, assets = _release("light-mindmap", "1.5.0", "https://github.com/ninglg/light-mindmap")
    downloads = {
        "https://api.github.com/repos/ninglg/light-mindmap/releases/latest": json.dumps(
            release
        ).encode(),
        **{
            f"https://github.com/example/light-mindmap/{name}": content
            for name, content in assets.items()
        },
    }
    original_replace = os.replace

    def fail_stage_replace(source: str | Path, destination: str | Path) -> None:
        if Path(source).name.startswith(".light-mindmap.staging-"):
            raise OSError("simulated staging replacement failure")
        original_replace(source, destination)

    monkeypatch.setattr(obsidian_plugins.os, "replace", fail_stage_replace)

    with pytest.raises(OSError, match="simulated staging replacement failure"):
        ObsidianPluginService(vault, download=downloads.__getitem__).install(["light-mindmap"])

    restored = json.loads((existing / "manifest.json").read_text(encoding="utf-8"))
    assert restored["name"] == "Old Light Mindmap"


def _local_context_graph_build(root: Path, version: str = "0.4.1") -> Path:
    source = root / "linked-graph-build"
    source.mkdir()
    (source / "main.js").write_text("module.exports = { version: 'new' };\n", encoding="utf-8")
    (source / "manifest.json").write_text(
        json.dumps(
            {
                "id": LINKED_GRAPH_ID,
                "name": "Linked Graph",
                "version": version,
                "minAppVersion": "1.8.0",
            }
        ),
        encoding="utf-8",
    )
    (source / "styles.css").write_text(".linked-graph { display: block; }\n", encoding="utf-8")
    subprocess.run(("git", "init", "-q", str(source)), check=True)
    subprocess.run(("git", "-C", str(source), "config", "user.name", "Woon Test"), check=True)
    subprocess.run(
        ("git", "-C", str(source), "config", "user.email", "test@example.invalid"),
        check=True,
    )
    subprocess.run(
        (
            "git",
            "-C",
            str(source),
            "remote",
            "add",
            "origin",
            "https://github.com/woonyong-choi/manta-graph.git",
        ),
        check=True,
    )
    subprocess.run(("git", "-C", str(source), "add", "."), check=True)
    subprocess.run(("git", "-C", str(source), "commit", "-q", "-m", "fixture"), check=True)
    return source


def _local_link_calendar_build(root: Path, version: str = LINK_CALENDAR_VERSION) -> Path:
    source = root / "link-calendar-build"
    source.mkdir()
    (source / "main.js").write_text("module.exports = { version: 'new' };\n", encoding="utf-8")
    (source / "manifest.json").write_text(
        json.dumps(
            {
                "id": LINK_CALENDAR_ID,
                "name": "Link Calendar",
                "version": version,
                "minAppVersion": "1.10.0",
            }
        ),
        encoding="utf-8",
    )
    (source / "styles.css").write_text(".link-calendar { display: block; }\n", encoding="utf-8")
    subprocess.run(("git", "init", "-q", str(source)), check=True)
    subprocess.run(("git", "-C", str(source), "config", "user.name", "Woon Test"), check=True)
    subprocess.run(
        ("git", "-C", str(source), "config", "user.email", "test@example.invalid"),
        check=True,
    )
    subprocess.run(
        (
            "git",
            "-C",
            str(source),
            "remote",
            "add",
            "origin",
            "https://github.com/woonyong-choi/manta-calendar.git",
        ),
        check=True,
    )
    subprocess.run(("git", "-C", str(source), "add", "."), check=True)
    subprocess.run(("git", "-C", str(source), "commit", "-q", "-m", "fixture"), check=True)
    return source


def _local_runnable_code_blocks_build(
    root: Path, version: str = RUNNABLE_CODE_BLOCKS_VERSION
) -> Path:
    source = root / "runnable-code-blocks-build"
    source.mkdir()
    (source / "main.js").write_text("module.exports = { version: 'new' };\n", encoding="utf-8")
    (source / "manifest.json").write_text(
        json.dumps(
            {
                "id": RUNNABLE_CODE_BLOCKS_ID,
                "name": "Runnable Code Blocks",
                "version": version,
                "minAppVersion": "1.13.0",
            }
        ),
        encoding="utf-8",
    )
    (source / "styles.css").write_text(".rcb { display: block; }\n", encoding="utf-8")
    subprocess.run(("git", "init", "-q", str(source)), check=True)
    subprocess.run(("git", "-C", str(source), "config", "user.name", "Woon Test"), check=True)
    subprocess.run(
        ("git", "-C", str(source), "config", "user.email", "test@example.invalid"),
        check=True,
    )
    subprocess.run(
        (
            "git",
            "-C",
            str(source),
            "remote",
            "add",
            "origin",
            "https://github.com/woonyong-choi/manta-code-blocks.git",
        ),
        check=True,
    )
    subprocess.run(("git", "-C", str(source), "add", "."), check=True)
    subprocess.run(("git", "-C", str(source), "commit", "-q", "-m", "fixture"), check=True)
    return source


def _install_link_calendar(vault: Path, build_root: Path) -> ObsidianPluginService:
    service = ObsidianPluginService(vault)
    service.install_local_build(
        LINK_CALENDAR_ID,
        _local_link_calendar_build(build_root),
        LINK_CALENDAR_VERSION,
    )
    settings = vault / ".obsidian/plugins/link-calendar/data.json"
    if not settings.exists():
        settings.write_text(
            json.dumps(
                {
                    "sourceProfiles": [{"id": "personal", "editable": False}],
                    "googleCalendar": {"sourceProfileIds": ["personal"]},
                }
            )
        )
    return service


def _attest_link_calendar_runtime(service: ObsidianPluginService) -> dict[str, object]:
    return service.attest_link_calendar_runtime(list(LINK_CALENDAR_MANUAL_ATTESTATION_CHECKS))


def test_install_local_build_preserves_settings_backup_hashes_and_enabled_config(
    tmp_path: Path,
) -> None:
    vault = _vault(tmp_path)
    destination = vault / ".obsidian/plugins" / LINKED_GRAPH_ID
    destination.mkdir()
    (destination / "main.js").write_text("old runtime\n", encoding="utf-8")
    (destination / "manifest.json").write_text(
        json.dumps({"id": LINKED_GRAPH_ID, "version": "0.4.0"}), encoding="utf-8"
    )
    (destination / "styles.css").write_text("old styles\n", encoding="utf-8")
    settings = b'{"defaultGraphId":"interview"}\n'
    (destination / "data.json").write_bytes(settings)
    source = _local_context_graph_build(tmp_path)

    receipt = ObsidianPluginService(vault).install_local_build(LINKED_GRAPH_ID, source, "0.4.1")

    assert receipt["action"] == "install-local-build"
    assert receipt["plugin"]["id"] == LINKED_GRAPH_ID
    assert receipt["plugin"]["version"] == "0.4.1"
    assert receipt["plugin"]["preserved_settings"] == ["data.json"]
    assert (
        receipt["plugin"]["assets_sha256"]["main.js"]
        == hashlib.sha256((source / "main.js").read_bytes()).hexdigest()
    )
    assert (destination / "main.js").read_bytes() == (source / "main.js").read_bytes()
    assert (destination / "data.json").read_bytes() == settings
    backup = vault / str(receipt["backup"])
    assert (backup / "main.js").read_text(encoding="utf-8") == "old runtime\n"
    assert LINKED_GRAPH_ID in json.loads(
        (vault / ".obsidian/community-plugins.json").read_text(encoding="utf-8")
    )
    assert list((vault / ".local/woon-knowledge/obsidian-plugins/receipts").glob("*.json"))


def test_runnable_code_blocks_local_build_uses_approved_git_source(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    source = _local_runnable_code_blocks_build(tmp_path)

    receipt = ObsidianPluginService(vault).install_local_build(
        RUNNABLE_CODE_BLOCKS_ID,
        source,
        RUNNABLE_CODE_BLOCKS_VERSION,
    )

    assert receipt["plugin"]["id"] == RUNNABLE_CODE_BLOCKS_ID
    assert receipt["plugin"]["version"] == RUNNABLE_CODE_BLOCKS_VERSION
    assert receipt["plugin"]["source"]["repository"] == (
        "https://github.com/woonyong-choi/manta-code-blocks.git"
    )
    assert RUNNABLE_CODE_BLOCKS_ID in json.loads(
        (vault / ".obsidian/community-plugins.json").read_text(encoding="utf-8")
    )


def test_install_local_build_keeps_plugin_runtime_state_user_only(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    source = _local_context_graph_build(tmp_path)

    ObsidianPluginService(vault).install_local_build(LINKED_GRAPH_ID, source, "0.4.1")

    runtime = vault / ".local/woon-knowledge/obsidian-plugins"
    for path in (runtime, *runtime.rglob("*")):
        expected = 0o700 if path.is_dir() else 0o600
        assert path.stat().st_mode & 0o777 == expected


def test_install_local_build_rejects_a_version_mismatch_before_mutating_the_vault(
    tmp_path: Path,
) -> None:
    vault = _vault(tmp_path)
    source = _local_context_graph_build(tmp_path, version="0.4.0")

    with pytest.raises(WoonError, match="version does not match"):
        ObsidianPluginService(vault).install_local_build(LINKED_GRAPH_ID, source, "0.4.1")

    assert not (vault / ".obsidian/plugins" / LINKED_GRAPH_ID).exists()
    assert json.loads((vault / ".obsidian/community-plugins.json").read_text(encoding="utf-8")) == [
        "homepage"
    ]


def test_install_local_build_preserves_destination_when_preflight_rejects_settings_entry(
    tmp_path: Path,
) -> None:
    vault = _vault(tmp_path)
    destination = vault / ".obsidian/plugins" / LINK_CALENDAR_ID
    destination.mkdir()
    (destination / "main.js").write_text("old runtime\n", encoding="utf-8")
    (destination / "manifest.json").write_text(
        json.dumps({"id": LINK_CALENDAR_ID, "version": "1.0.0"}), encoding="utf-8"
    )
    (destination / "styles.css").write_text("old styles\n", encoding="utf-8")
    nested_settings = destination / "nested-settings"
    nested_settings.mkdir()
    (nested_settings / "state.json").write_text('{"kept":true}\n', encoding="utf-8")
    enabled_before = (vault / ".obsidian/community-plugins.json").read_bytes()

    with pytest.raises(WoonError, match="unsupported settings entry"):
        ObsidianPluginService(vault).install_local_build(
            LINK_CALENDAR_ID,
            _local_link_calendar_build(tmp_path),
            LINK_CALENDAR_VERSION,
        )

    assert (destination / "main.js").read_text(encoding="utf-8") == "old runtime\n"
    assert (nested_settings / "state.json").read_text(encoding="utf-8") == '{"kept":true}\n'
    assert (vault / ".obsidian/community-plugins.json").read_bytes() == enabled_before


def test_install_local_build_accepts_link_calendar_at_the_pinned_version(
    tmp_path: Path,
) -> None:
    vault = _vault(tmp_path)

    receipt = ObsidianPluginService(vault).install_local_build(
        LINK_CALENDAR_ID,
        _local_link_calendar_build(tmp_path),
        LINK_CALENDAR_VERSION,
    )

    assert receipt["plugin"]["id"] == LINK_CALENDAR_ID
    assert receipt["plugin"]["version"] == LINK_CALENDAR_VERSION
    assert receipt["plugin"]["source"]["repository"] == (
        "https://github.com/woonyong-choi/manta-calendar.git"
    )
    assert len(receipt["plugin"]["source"]["head_commit"]) == 40
    assert receipt["plugin"]["source"]["clean"] is True
    assert LINK_CALENDAR_ID in json.loads(
        (vault / ".obsidian/community-plugins.json").read_text(encoding="utf-8")
    )


@pytest.mark.parametrize("failure", ["dirty", "wrong-origin", "not-git"])
def test_link_calendar_local_build_requires_approved_clean_git_provenance(
    tmp_path: Path, failure: str
) -> None:
    vault = _vault(tmp_path)
    source = _local_link_calendar_build(tmp_path)
    if failure == "dirty":
        (source / "main.js").write_text("dirty runtime\n", encoding="utf-8")
        expected = "must be clean"
    elif failure == "wrong-origin":
        subprocess.run(
            ("git", "-C", str(source), "remote", "set-url", "origin", "https://example.com/x.git"),
            check=True,
        )
        expected = "origin is not approved"
    else:
        (source / ".git").rename(source / ".not-git")
        expected = "Git provenance is invalid"

    with pytest.raises(WoonError, match=expected):
        ObsidianPluginService(vault).install_local_build(
            LINK_CALENDAR_ID,
            source,
            LINK_CALENDAR_VERSION,
        )

    assert not (vault / ".obsidian/plugins" / LINK_CALENDAR_ID).exists()


def test_link_calendar_git_origin_accepts_the_normalized_url_without_git_suffix(
    tmp_path: Path,
) -> None:
    vault = _vault(tmp_path)
    source = _local_link_calendar_build(tmp_path)
    subprocess.run(
        (
            "git",
            "-C",
            str(source),
            "remote",
            "set-url",
            "origin",
            "https://github.com/woonyong-choi/manta-calendar",
        ),
        check=True,
    )

    receipt = ObsidianPluginService(vault).install_local_build(
        LINK_CALENDAR_ID,
        source,
        LINK_CALENDAR_VERSION,
    )

    assert receipt["plugin"]["source"]["repository"].endswith("manta-calendar.git")


@pytest.mark.parametrize("failure", ["dirty", "wrong-origin", "not-git"])
def test_context_graph_local_build_requires_approved_clean_git_provenance(
    tmp_path: Path, failure: str
) -> None:
    vault = _vault(tmp_path)
    source = _local_context_graph_build(tmp_path)
    if failure == "dirty":
        (source / "main.js").write_text("dirty runtime\n", encoding="utf-8")
        expected = "must be clean"
    elif failure == "wrong-origin":
        subprocess.run(
            ("git", "-C", str(source), "remote", "set-url", "origin", "https://example.com/x.git"),
            check=True,
        )
        expected = "origin is not approved"
    else:
        (source / ".git").rename(source / ".not-git")
        expected = "Git provenance is invalid"

    with pytest.raises(WoonError, match=expected):
        ObsidianPluginService(vault).install_local_build(LINKED_GRAPH_ID, source, "0.4.1")

    assert not (vault / ".obsidian/plugins" / LINKED_GRAPH_ID).exists()


@pytest.mark.parametrize("linked_root", ["obsidian", "plugins", "local", "receipts"])
def test_plugin_mutations_reject_control_path_symlinks_before_lock_or_write(
    tmp_path: Path, linked_root: str
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    vault = tmp_path / "vault"
    if linked_root == "obsidian":
        vault.mkdir()
        (outside / "plugins").mkdir()
        (outside / "community-plugins.json").write_text("[]\n", encoding="utf-8")
        (vault / ".obsidian").symlink_to(outside, target_is_directory=True)
    else:
        vault = _vault(tmp_path)
        if linked_root == "plugins":
            (vault / ".obsidian/plugins").rmdir()
            (vault / ".obsidian/plugins").symlink_to(outside, target_is_directory=True)
        elif linked_root == "local":
            (vault / ".local").symlink_to(outside, target_is_directory=True)
        else:
            local = vault / ".local/woon-knowledge/obsidian-plugins"
            local.mkdir(parents=True)
            (local / "receipts").symlink_to(outside, target_is_directory=True)

    with pytest.raises(WoonError, match="regular Vault directory"):
        ObsidianPluginService(vault).install_local_build(
            LINKED_GRAPH_ID,
            _local_context_graph_build(tmp_path),
            "0.4.1",
        )

    assert not (outside / LINKED_GRAPH_ID).exists()
    assert not (outside / "woon-knowledge/obsidian-plugins/mutation.lock").exists()
    assert not (vault / ".local/woon-knowledge/obsidian-plugins/mutation.lock").exists()


def test_install_local_build_restores_runtime_settings_and_enabled_config_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = _vault(tmp_path)
    destination = vault / ".obsidian/plugins" / LINKED_GRAPH_ID
    destination.mkdir()
    (destination / "main.js").write_text("old runtime\n", encoding="utf-8")
    (destination / "manifest.json").write_text(
        json.dumps({"id": LINKED_GRAPH_ID, "version": "0.4.0"}), encoding="utf-8"
    )
    (destination / "styles.css").write_text("old styles\n", encoding="utf-8")
    (destination / "data.json").write_text('{"kept":true}\n', encoding="utf-8")
    enabled_before = (vault / ".obsidian/community-plugins.json").read_bytes()
    source = _local_context_graph_build(tmp_path)
    service = ObsidianPluginService(vault)

    def fail_enabled_write(enabled: set[str], backup_root: Path) -> None:
        raise OSError("simulated enabled config failure")

    monkeypatch.setattr(service, "_write_enabled_ids", fail_enabled_write)

    with pytest.raises(OSError, match="simulated enabled config failure"):
        service.install_local_build(LINKED_GRAPH_ID, source, "0.4.1")

    assert (destination / "main.js").read_text(encoding="utf-8") == "old runtime\n"
    assert (destination / "data.json").read_text(encoding="utf-8") == '{"kept":true}\n'
    assert (vault / ".obsidian/community-plugins.json").read_bytes() == enabled_before


def test_install_local_build_rolls_back_if_receipt_cannot_be_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = _vault(tmp_path)
    destination = vault / ".obsidian/plugins" / LINKED_GRAPH_ID
    destination.mkdir()
    (destination / "main.js").write_text("old runtime\n", encoding="utf-8")
    (destination / "manifest.json").write_text(
        json.dumps({"id": LINKED_GRAPH_ID, "version": "0.4.0"}), encoding="utf-8"
    )
    (destination / "styles.css").write_text("old styles\n", encoding="utf-8")
    (destination / "data.json").write_text('{"kept":true}\n', encoding="utf-8")
    enabled_before = (vault / ".obsidian/community-plugins.json").read_bytes()
    source = _local_context_graph_build(tmp_path)
    atomic_write = obsidian_plugins._atomic_write

    def fail_receipt_write(path: Path, content: bytes) -> None:
        if "receipts" in path.parts:
            raise OSError("simulated receipt failure")
        atomic_write(path, content)

    monkeypatch.setattr(obsidian_plugins, "_atomic_write", fail_receipt_write)

    with pytest.raises(OSError, match="simulated receipt failure"):
        ObsidianPluginService(vault).install_local_build(LINKED_GRAPH_ID, source, "0.4.1")

    assert (destination / "main.js").read_text(encoding="utf-8") == "old runtime\n"
    assert (destination / "data.json").read_text(encoding="utf-8") == '{"kept":true}\n'
    assert (vault / ".obsidian/community-plugins.json").read_bytes() == enabled_before


def test_install_local_build_refuses_destructive_rollback_after_plugin_tree_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = _vault(tmp_path)
    destination = vault / ".obsidian/plugins" / LINKED_GRAPH_ID
    destination.mkdir()
    (destination / "main.js").write_text("old runtime\n", encoding="utf-8")
    (destination / "manifest.json").write_text(
        json.dumps({"id": LINKED_GRAPH_ID, "version": "0.4.0"}), encoding="utf-8"
    )
    (destination / "styles.css").write_text("old styles\n", encoding="utf-8")
    atomic_write = obsidian_plugins._atomic_write

    def fail_receipt_after_drift(path: Path, content: bytes) -> None:
        if "receipts" in path.parts:
            (destination / "concurrent.json").write_text('{"owner":"Obsidian"}\n', encoding="utf-8")
            raise OSError("simulated receipt failure after plugin drift")
        atomic_write(path, content)

    monkeypatch.setattr(obsidian_plugins, "_atomic_write", fail_receipt_after_drift)

    with pytest.raises(WoonError, match="destructive rollback refused"):
        ObsidianPluginService(vault).install_local_build(
            LINKED_GRAPH_ID,
            _local_context_graph_build(tmp_path),
            "0.4.1",
        )

    assert (destination / "concurrent.json").is_file()
    assert list((vault / ".local/woon-knowledge/obsidian-plugins/backups").rglob("main.js"))


def test_install_local_build_preserves_concurrent_enabled_config_during_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = _vault(tmp_path)
    destination = vault / ".obsidian/plugins" / LINKED_GRAPH_ID
    destination.mkdir()
    (destination / "main.js").write_text("old runtime\n", encoding="utf-8")
    (destination / "manifest.json").write_text(
        json.dumps({"id": LINKED_GRAPH_ID, "version": "0.4.0"}), encoding="utf-8"
    )
    (destination / "styles.css").write_text("old styles\n", encoding="utf-8")
    enabled_path = vault / ".obsidian/community-plugins.json"
    concurrent = b'["homepage", "concurrent-plugin"]\n'
    atomic_write = obsidian_plugins._atomic_write

    def fail_receipt_after_enabled_drift(path: Path, content: bytes) -> None:
        if "receipts" in path.parts:
            enabled_path.write_bytes(concurrent)
            raise OSError("simulated receipt failure after enabled drift")
        atomic_write(path, content)

    monkeypatch.setattr(obsidian_plugins, "_atomic_write", fail_receipt_after_enabled_drift)

    with pytest.raises(WoonError, match="rollback refused"):
        ObsidianPluginService(vault).install_local_build(
            LINKED_GRAPH_ID,
            _local_context_graph_build(tmp_path),
            "0.4.1",
        )

    assert enabled_path.read_bytes() == concurrent
    assert (destination / "main.js").read_text(encoding="utf-8") == "old runtime\n"


def test_link_calendar_static_gate_rejects_install_receipt_without_git_provenance(
    tmp_path: Path,
) -> None:
    vault = _vault(tmp_path)
    service = _install_link_calendar(vault, tmp_path)
    receipt_root = vault / ".local/woon-knowledge/obsidian-plugins/receipts"
    install_receipt = next(
        path
        for path in receipt_root.glob("*.json")
        if json.loads(path.read_text(encoding="utf-8"))["action"] == "install-local-build"
    )
    payload = json.loads(install_receipt.read_text(encoding="utf-8"))
    payload["plugin"].pop("source")
    install_receipt.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(WoonError, match="verified local-build adapter"):
        _attest_link_calendar_runtime(service)


def test_attest_link_calendar_runtime_requires_complete_explicit_checklist(
    tmp_path: Path,
) -> None:
    vault = _vault(tmp_path)
    service = _install_link_calendar(vault, tmp_path)

    with pytest.raises(WoonError, match="complete UI checklist"):
        service.attest_link_calendar_runtime(["ribbon", "month-view"])

    assert not any(
        json.loads(path.read_text(encoding="utf-8")).get("action") == "attest-link-calendar-runtime"
        for path in (vault / ".local/woon-knowledge/obsidian-plugins/receipts").glob("*.json")
    )


def test_attest_link_calendar_runtime_receipt_binds_current_static_evidence(
    tmp_path: Path,
) -> None:
    vault = _vault(tmp_path)
    service = _install_link_calendar(vault, tmp_path)

    receipt = _attest_link_calendar_runtime(service)

    assert receipt["action"] == "attest-link-calendar-runtime"
    assert receipt["operator_attested_checks"] == list(LINK_CALENDAR_MANUAL_ATTESTATION_CHECKS)
    assert receipt["attestation"] == "manual-operator-confirmation-after-Obsidian-reload"
    assert receipt["plugin"]["assets_sha256"]
    assert receipt["settings"]["sha256"]
    assert "dashboard" not in receipt


def test_retire_legacy_simple_calendar_requires_manual_runtime_attestation(
    tmp_path: Path,
) -> None:
    vault = _vault(tmp_path)
    legacy = vault / ".obsidian/plugins" / LEGACY_SIMPLE_CALENDAR_ID
    legacy.mkdir()
    (legacy / "manifest.json").write_text(
        json.dumps({"id": LEGACY_SIMPLE_CALENDAR_ID, "version": "1.1.1"}), encoding="utf-8"
    )
    service = _install_link_calendar(vault, tmp_path)

    with pytest.raises(WoonError, match="manual operator attestation after reload"):
        service.retire([LEGACY_SIMPLE_CALENDAR_ID])

    assert legacy.is_dir()

    assert legacy.is_dir()


def test_retire_context_graph_requires_receipted_linked_graph(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    legacy = vault / ".obsidian/plugins" / LEGACY_CONTEXT_GRAPH_ID
    legacy.mkdir()
    (legacy / "manifest.json").write_text(
        json.dumps({"id": LEGACY_CONTEXT_GRAPH_ID, "version": "0.5.14"}), encoding="utf-8"
    )

    with pytest.raises(WoonError, match="linked-graph"):
        ObsidianPluginService(vault).retire([LEGACY_CONTEXT_GRAPH_ID])

    assert legacy.is_dir()


def test_retire_context_graph_keeps_backup_after_linked_graph_validates(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    legacy = vault / ".obsidian/plugins" / LEGACY_CONTEXT_GRAPH_ID
    legacy.mkdir()
    (legacy / "manifest.json").write_text(
        json.dumps({"id": LEGACY_CONTEXT_GRAPH_ID, "version": "0.5.14"}), encoding="utf-8"
    )
    (legacy / "main.js").write_text("legacy runtime\n", encoding="utf-8")
    service = ObsidianPluginService(vault)
    service.install_local_build(
        LINKED_GRAPH_ID,
        _local_context_graph_build(tmp_path, version=LINKED_GRAPH_VERSION),
        LINKED_GRAPH_VERSION,
    )

    receipt = service.retire([LEGACY_CONTEXT_GRAPH_ID])

    assert receipt["retired"] == [LEGACY_CONTEXT_GRAPH_ID]
    assert not legacy.exists()
    backup = (
        vault
        / ".local/woon-knowledge/obsidian-plugins/backups"
        / receipt["receipt_id"]
        / LEGACY_CONTEXT_GRAPH_ID
    )
    assert (backup / "main.js").read_text(encoding="utf-8") == "legacy runtime\n"


def test_retire_notion_bases_requires_link_calendar_runtime(
    tmp_path: Path,
) -> None:
    vault = _vault(tmp_path)
    plugin = vault / ".obsidian/plugins" / NOTION_BASES_ID
    plugin.mkdir()
    (plugin / "manifest.json").write_text(
        json.dumps({"id": NOTION_BASES_ID, "version": "1.12.0"}), encoding="utf-8"
    )
    (vault / ".obsidian/community-plugins.json").write_text(
        json.dumps([NOTION_BASES_ID]), encoding="utf-8"
    )

    with pytest.raises(WoonError, match="link-calendar"):
        ObsidianPluginService(vault).retire([NOTION_BASES_ID])


def test_retire_notion_bases_keeps_a_backup_after_link_calendar_validates(
    tmp_path: Path,
) -> None:
    vault = _vault(tmp_path)
    notion_bases = vault / ".obsidian/plugins" / NOTION_BASES_ID
    notion_bases.mkdir()
    (notion_bases / "manifest.json").write_text(
        json.dumps({"id": NOTION_BASES_ID, "version": "1.12.0"}), encoding="utf-8"
    )
    service = _install_link_calendar(vault, tmp_path)
    _attest_link_calendar_runtime(service)

    receipt = service.retire([NOTION_BASES_ID])

    assert receipt["retired"] == [NOTION_BASES_ID]
    assert not notion_bases.exists()
    assert list((vault / ".local/woon-knowledge/obsidian-plugins/backups").rglob(NOTION_BASES_ID))


def test_retire_legacy_simple_calendar_requires_verified_link_calendar(
    tmp_path: Path,
) -> None:
    vault = _vault(tmp_path)
    legacy = vault / ".obsidian/plugins" / LEGACY_SIMPLE_CALENDAR_ID
    legacy.mkdir()
    (legacy / "manifest.json").write_text(
        json.dumps({"id": LEGACY_SIMPLE_CALENDAR_ID, "version": "1.1.1"}), encoding="utf-8"
    )

    with pytest.raises(WoonError, match="link-calendar"):
        ObsidianPluginService(vault).retire([LEGACY_SIMPLE_CALENDAR_ID])

    assert legacy.is_dir()


def test_retire_legacy_simple_calendar_rejects_settings_drift_after_configuration(
    tmp_path: Path,
) -> None:
    vault = _vault(tmp_path)
    legacy = vault / ".obsidian/plugins" / LEGACY_SIMPLE_CALENDAR_ID
    legacy.mkdir()
    (legacy / "manifest.json").write_text(
        json.dumps({"id": LEGACY_SIMPLE_CALENDAR_ID, "version": "1.1.1"}), encoding="utf-8"
    )
    service = _install_link_calendar(vault, tmp_path)
    _attest_link_calendar_runtime(service)
    settings = vault / ".obsidian/plugins" / LINK_CALENDAR_ID / "data.json"
    configuration = json.loads(settings.read_text(encoding="utf-8"))
    configuration["sourceProfiles"][-1]["editable"] = True
    settings.write_text(json.dumps(configuration), encoding="utf-8")

    with pytest.raises(WoonError, match="manual operator attestation"):
        service.retire([LEGACY_SIMPLE_CALENDAR_ID])

    assert legacy.is_dir()


def test_retire_legacy_simple_calendar_keeps_backup_after_all_guards_pass(
    tmp_path: Path,
) -> None:
    vault = _vault(tmp_path)
    legacy = vault / ".obsidian/plugins" / LEGACY_SIMPLE_CALENDAR_ID
    legacy.mkdir()
    (legacy / "manifest.json").write_text(
        json.dumps({"id": LEGACY_SIMPLE_CALENDAR_ID, "version": "1.1.1"}), encoding="utf-8"
    )
    (vault / ".obsidian/community-plugins.json").write_text(
        json.dumps(["homepage", LEGACY_SIMPLE_CALENDAR_ID]), encoding="utf-8"
    )
    service = _install_link_calendar(vault, tmp_path)
    _attest_link_calendar_runtime(service)

    receipt = service.retire([LEGACY_SIMPLE_CALENDAR_ID])

    assert receipt["retired"] == [LEGACY_SIMPLE_CALENDAR_ID]
    assert not legacy.exists()
    assert LEGACY_SIMPLE_CALENDAR_ID not in json.loads(
        (vault / ".obsidian/community-plugins.json").read_text(encoding="utf-8")
    )
    assert list(
        (vault / ".local/woon-knowledge/obsidian-plugins/backups").rglob(LEGACY_SIMPLE_CALENDAR_ID)
    )


def test_retire_context_calendar_keeps_backup_after_link_calendar_is_verified(
    tmp_path: Path,
) -> None:
    vault = _vault(tmp_path)
    legacy = vault / ".obsidian/plugins" / LEGACY_CONTEXT_CALENDAR_ID
    legacy.mkdir()
    (legacy / "manifest.json").write_text(
        json.dumps({"id": LEGACY_CONTEXT_CALENDAR_ID, "version": "2.1.3"}), encoding="utf-8"
    )
    (legacy / "data.json").write_text('{"showContext":true}\n', encoding="utf-8")
    (vault / ".obsidian/community-plugins.json").write_text(
        json.dumps(["homepage", LEGACY_CONTEXT_CALENDAR_ID]), encoding="utf-8"
    )
    service = _install_link_calendar(vault, tmp_path)
    _attest_link_calendar_runtime(service)

    receipt = service.retire([LEGACY_CONTEXT_CALENDAR_ID])

    assert receipt["retired"] == [LEGACY_CONTEXT_CALENDAR_ID]
    assert not legacy.exists()
    assert LEGACY_CONTEXT_CALENDAR_ID not in json.loads(
        (vault / ".obsidian/community-plugins.json").read_text(encoding="utf-8")
    )
    backup_root = vault / ".local/woon-knowledge/obsidian-plugins/backups"
    assert list(backup_root.rglob(LEGACY_CONTEXT_CALENDAR_ID))


def test_retire_rolls_back_plugin_and_enabled_config_when_receipt_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = _vault(tmp_path)
    legacy = vault / ".obsidian/plugins" / LEGACY_SIMPLE_CALENDAR_ID
    legacy.mkdir()
    manifest = json.dumps({"id": LEGACY_SIMPLE_CALENDAR_ID, "version": "1.1.1"})
    (legacy / "manifest.json").write_text(manifest, encoding="utf-8")
    enabled_path = vault / ".obsidian/community-plugins.json"
    enabled_path.write_text(json.dumps(["homepage", LEGACY_SIMPLE_CALENDAR_ID]), encoding="utf-8")
    service = _install_link_calendar(vault, tmp_path)
    _attest_link_calendar_runtime(service)
    enabled_before = enabled_path.read_bytes()
    atomic_write = obsidian_plugins._atomic_write

    def fail_retire_receipt(path: Path, content: bytes) -> None:
        if path.parent.name == "receipts" and b'"action": "retire"' in content:
            raise OSError("simulated retire receipt failure")
        atomic_write(path, content)

    monkeypatch.setattr(obsidian_plugins, "_atomic_write", fail_retire_receipt)

    with pytest.raises(OSError, match="simulated retire receipt failure"):
        service.retire([LEGACY_SIMPLE_CALENDAR_ID])

    assert (legacy / "manifest.json").read_text(encoding="utf-8") == manifest
    assert enabled_path.read_bytes() == enabled_before
    assert not any(
        json.loads(path.read_text(encoding="utf-8")).get("action") == "retire"
        for path in (vault / ".local/woon-knowledge/obsidian-plugins/receipts").glob("*.json")
    )


def test_retire_rejects_a_directory_whose_manifest_id_does_not_match(
    tmp_path: Path,
) -> None:
    vault = _vault(tmp_path)
    legacy = vault / ".obsidian/plugins" / LEGACY_SIMPLE_CALENDAR_ID
    legacy.mkdir()
    (legacy / "manifest.json").write_text(
        json.dumps({"id": "unrelated-plugin", "version": "1.0.0"}), encoding="utf-8"
    )
    service = _install_link_calendar(vault, tmp_path)
    _attest_link_calendar_runtime(service)

    with pytest.raises(WoonError, match="manifest is invalid"):
        service.retire([LEGACY_SIMPLE_CALENDAR_ID])

    assert legacy.is_dir()


def test_retire_full_calendar_requires_link_calendar_runtime(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    plugin = vault / ".obsidian/plugins" / FULL_CALENDAR_REMASTERED_ID
    plugin.mkdir()
    (plugin / "manifest.json").write_text(
        json.dumps({"id": FULL_CALENDAR_REMASTERED_ID, "version": "0.13.5"}), encoding="utf-8"
    )
    (vault / ".obsidian/community-plugins.json").write_text(
        json.dumps(["homepage", FULL_CALENDAR_REMASTERED_ID]), encoding="utf-8"
    )

    with pytest.raises(WoonError, match="link-calendar"):
        ObsidianPluginService(vault).retire([FULL_CALENDAR_REMASTERED_ID])


def test_retire_full_calendar_keeps_a_local_backup_after_link_calendar_validates(
    tmp_path: Path,
) -> None:
    vault = _vault(tmp_path)
    plugins = vault / ".obsidian/plugins"
    full_calendar = plugins / FULL_CALENDAR_REMASTERED_ID
    full_calendar.mkdir()
    (full_calendar / "manifest.json").write_text(
        json.dumps({"id": FULL_CALENDAR_REMASTERED_ID, "version": "0.13.5"}), encoding="utf-8"
    )
    notion_bases = plugins / NOTION_BASES_ID
    notion_bases.mkdir()
    (notion_bases / "manifest.json").write_text(
        json.dumps({"id": NOTION_BASES_ID, "version": "1.12.0"}), encoding="utf-8"
    )
    (vault / ".obsidian/community-plugins.json").write_text(
        json.dumps(["homepage", FULL_CALENDAR_REMASTERED_ID, NOTION_BASES_ID]),
        encoding="utf-8",
    )
    service = _install_link_calendar(vault, tmp_path)
    _attest_link_calendar_runtime(service)

    receipt = ObsidianPluginService(vault).retire([FULL_CALENDAR_REMASTERED_ID])

    assert receipt["retired"] == [FULL_CALENDAR_REMASTERED_ID]
    assert not full_calendar.exists()
    assert FULL_CALENDAR_REMASTERED_ID not in json.loads(
        (vault / ".obsidian/community-plugins.json").read_text()
    )
    assert list(
        (vault / ".local/woon-knowledge/obsidian-plugins/backups").rglob(
            FULL_CALENDAR_REMASTERED_ID
        )
    )


def test_retire_moves_only_the_explicit_legacy_calendar_renderer(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    plugins = vault / ".obsidian/plugins"
    legacy = plugins / PRISMA_CALENDAR_ID
    legacy.mkdir()
    (legacy / "manifest.json").write_text(
        json.dumps({"id": PRISMA_CALENDAR_ID, "version": "2.22.0"}), encoding="utf-8"
    )
    unrelated = plugins / "homepage"
    unrelated.mkdir()
    (unrelated / "manifest.json").write_text(
        json.dumps({"id": "homepage", "version": "1.0.0"}), encoding="utf-8"
    )
    (vault / ".obsidian/community-plugins.json").write_text(
        json.dumps([PRISMA_CALENDAR_ID, "homepage"]), encoding="utf-8"
    )

    receipt = ObsidianPluginService(vault).retire([PRISMA_CALENDAR_ID])

    assert receipt["retired"] == [PRISMA_CALENDAR_ID]
    assert not legacy.exists()
    assert unrelated.is_dir()
    assert PRISMA_CALENDAR_ID not in json.loads(
        (vault / ".obsidian/community-plugins.json").read_text()
    )
    assert list(
        (vault / ".local/woon-knowledge/obsidian-plugins/backups").rglob(PRISMA_CALENDAR_ID)
    )
