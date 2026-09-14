from pathlib import Path

import pytest

from woon_core.errors import WoonError
from woon_core.registry import Registry, Repository


def registry() -> Registry:
    return Registry(
        version=1,
        repositories={
            "knowledge": Repository(
                remote="https://github.com/example/knowledge.git",
                directory="woon-knowledge",
            )
        },
    )


def test_resolve_repo_uri(tmp_path: Path) -> None:
    resolved = registry().resolve(tmp_path, "repo://knowledge/wiki/os/page.md")
    assert resolved == tmp_path / "woon-knowledge/wiki/os/page.md"


def test_resolve_rejects_escape(tmp_path: Path) -> None:
    with pytest.raises(WoonError, match="may not escape"):
        registry().resolve(tmp_path, "repo://knowledge/../secret")


def test_validate_rejects_absolute_directory() -> None:
    invalid = Registry(
        version=1,
        repositories={
            "knowledge": Repository(
                remote="https://github.com/example/knowledge.git", directory="/absolute"
            )
        },
    )
    with pytest.raises(WoonError, match="unsafe directory"):
        invalid.validate()


def test_sibling_repository_resolution_preserves_escape_boundary(tmp_path: Path) -> None:
    sibling = Registry(
        version=1,
        repositories={
            "calendar": Repository(
                remote="https://github.com/example/calendar.git",
                directory="OSS/obsidian/calendar",
                base="workspace-parent",
            )
        },
    )
    sibling.validate()
    assert sibling.resolve(tmp_path / "woon", "repo://calendar/manifest.json") == (
        tmp_path / "OSS/obsidian/calendar/manifest.json"
    )
    with pytest.raises(WoonError, match="may not escape"):
        sibling.resolve(tmp_path / "woon", "repo://calendar/../other")


def test_registry_rejects_unknown_base_and_parent_directory() -> None:
    for base, directory, reason in [
        ("outside", "calendar", "unsupported base"),
        ("workspace-parent", "../calendar", "unsafe directory"),
    ]:
        invalid = Registry(
            version=1,
            repositories={
                "calendar": Repository(
                    remote="https://github.com/example/calendar.git",
                    directory=directory,
                    base=base,
                )
            },
        )
        with pytest.raises(WoonError, match=reason):
            invalid.validate()
