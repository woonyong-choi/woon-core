"""Repository registry validation and path resolution."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

from woon_core.errors import WoonError
from woon_core.io import load_yaml

REGISTRY_RELATIVE_PATH = Path("woon-core/registry/repositories.yaml")


@dataclass(frozen=True, slots=True)
class Repository:
    remote: str
    directory: str
    role: str = ""
    output: bool = False
    local_only: bool = False
    context_managed: bool = True
    base: str = "workspace"

    def path(self, root: Path) -> Path:
        anchor = root.parent if self.base == "workspace-parent" else root
        return anchor / self.directory


@dataclass(frozen=True, slots=True)
class SyncResult:
    cloned: int
    existing: int


@dataclass(frozen=True, slots=True)
class Registry:
    version: int
    repositories: dict[str, Repository]

    @classmethod
    def load(cls, root: Path) -> Registry:
        path = root / REGISTRY_RELATIVE_PATH
        raw = load_yaml(path)
        raw_repositories = raw.get("repositories")
        if not isinstance(raw_repositories, dict):
            raise WoonError("registry requires a repositories mapping")
        repositories: dict[str, Repository] = {}
        for identifier, item in raw_repositories.items():
            if not isinstance(identifier, str) or not isinstance(item, dict):
                raise WoonError("registry repository entries must be mappings")
            repositories[identifier] = Repository(
                remote=str(item.get("remote", "")),
                directory=str(item.get("directory", "")),
                role=str(item.get("role", "")),
                output=bool(item.get("output", False)),
                local_only=bool(item.get("local_only", False)),
                context_managed=bool(item.get("context_managed", True)),
                base=str(item.get("base", "workspace")),
            )
        registry = cls(version=int(raw.get("version", 0)), repositories=repositories)
        registry.validate()
        return registry

    def validate(self) -> None:
        if self.version != 1:
            raise WoonError(f"unsupported registry version {self.version}")
        seen_directories: dict[tuple[str, str], str] = {}
        for identifier, repository in self.repositories.items():
            if repository.base not in {"workspace", "workspace-parent"}:
                raise WoonError(
                    f"repository {identifier!r} has unsupported base {repository.base!r}"
                )
            if (
                not identifier
                or not repository.directory
                or (not repository.remote and not repository.local_only)
            ):
                raise WoonError(f"repository {identifier!r} requires remote and directory")
            directory = PurePosixPath(repository.directory)
            if (
                directory.is_absolute()
                or ".." in directory.parts
                or str(directory) != repository.directory
            ):
                raise WoonError(
                    f"repository {identifier!r} has unsafe directory {repository.directory!r}"
                )
            location = (repository.base, repository.directory)
            if previous := seen_directories.get(location):
                raise WoonError(
                    f"repositories {previous!r} and {identifier!r} share directory "
                    f"{repository.directory!r}"
                )
            seen_directories[location] = identifier
            if repository.local_only:
                if repository.remote:
                    raise WoonError(f"local repository {identifier!r} must not declare a remote")
                continue
            parsed = urlparse(repository.remote)
            if parsed.scheme != "https" or parsed.hostname != "github.com":
                raise WoonError(
                    f"repository {identifier!r} has unsupported remote {repository.remote!r}"
                )

    def resolve(self, root: Path, reference: str, *, must_exist: bool = False) -> Path:
        """Resolve a safe reference; readers can require a present target.

        The default also supports generator destinations that do not exist yet.
        CLI lookups require existence and never invent a replacement repository.
        """
        identifier, relative = _parse_reference(reference)
        try:
            repository = self.repositories[identifier]
        except KeyError as error:
            raise WoonError(f"unknown repository {identifier!r}") from error
        base = repository.path(root).resolve(strict=False)
        resolved = (base / relative).resolve(strict=False)
        if not resolved.is_relative_to(base):
            raise WoonError(f"reference escapes repository {identifier!r}")
        if must_exist and not resolved.exists():
            raise WoonError(f"repository reference does not exist: {reference}")
        return resolved

    def missing(self, root: Path) -> list[str]:
        return sorted(
            identifier
            for identifier, repository in self.repositories.items()
            if not repository.path(root).exists()
        )

    def sync(self, root: Path) -> SyncResult:
        cloned = 0
        existing = 0
        for identifier in sorted(self.repositories):
            repository = self.repositories[identifier]
            target = repository.path(root)
            if target.exists():
                if not (target / ".git").exists():
                    raise WoonError(f"{target} exists but is not a Git checkout")
                existing += 1
                continue
            if repository.local_only:
                raise WoonError(f"local repository {identifier!r} requires local setup")
            try:
                subprocess.run(["git", "clone", "--", repository.remote, str(target)], check=True)
            except subprocess.CalledProcessError as error:
                raise WoonError(f"clone {identifier}: {error}") from error
            cloned += 1
        return SyncResult(cloned=cloned, existing=existing)


def _parse_reference(reference: str) -> tuple[str, Path]:
    if not reference.startswith("repo://"):
        if not reference or "/" in reference:
            raise WoonError(f"invalid repository ID {reference!r}")
        return reference, Path()
    rest = reference.removeprefix("repo://")
    identifier, separator, raw_relative = rest.partition("/")
    if not identifier:
        raise WoonError("repo URI requires an ID")
    relative = PurePosixPath(raw_relative) if separator else PurePosixPath()
    if relative.is_absolute() or ".." in relative.parts:
        raise WoonError("repo URI may not escape its repository")
    return identifier, Path(*relative.parts)
