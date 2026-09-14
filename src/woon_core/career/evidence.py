"""Read-only, revision-pinned career evidence; never infer authorship from code."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from woon_core.errors import WoonError
from woon_core.registry import Registry
from woon_core.workspace import discover


def digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
        ).encode()
    ).hexdigest()


def bytes_digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def wiki_path(vault: Path, value: str) -> Path:
    """Resolve a canonical page without following a link outside the Wiki."""
    path = PurePosixPath(value)
    if (
        not value.startswith("wiki/")
        or path.suffix != ".md"
        or str(path) != value
        or ".." in path.parts
        or "\\" in value
        or any(c in value for c in "\r\n#|")
    ):
        raise WoonError("career evidence must be an existing wiki Markdown path")
    result = (vault / value).resolve()
    root = (vault / "wiki").resolve()
    if not root.is_relative_to(vault) or not result.is_relative_to(root):
        raise WoonError("career evidence escapes its canonical Wiki root")
    if value.startswith(("wiki/personal/career/applications/", "wiki/private/_sources/")):
        raise WoonError("generated applications and raw sources are not career evidence")
    if not result.is_file():
        raise WoonError(f"career evidence does not exist: {value}")
    return result


def section_text(body: str, heading: str) -> str:
    """Select one exact heading and its descendants; duplicate headings fail closed."""
    lines = body.splitlines(keepends=True)
    headings: list[tuple[int, int, str]] = []
    fence: str | None = None
    for index, line in enumerate(lines):
        marker = re.match(r"^\s{0,3}(`{3,}|~{3,})", line)
        if marker:
            token = marker[1]
            if fence is None:
                fence = token
            elif (
                token[0] == fence[0]
                and len(token) >= len(fence)
                and not line[marker.end() :].strip()
            ):
                fence = None
            continue
        if fence is None and (match := re.match(r"^(#{1,6})[ \t]+(.+?)\s*$", line)):
            title = re.sub(r"[ \t]+#+[ \t]*$", "", match[2]).strip()
            headings.append((index, len(match[1]), title))
    matches = [entry for entry in headings if entry[2] == heading]
    if len(matches) != 1:
        raise WoonError("career section must match one unambiguous canonical heading")
    start, level, _ = matches[0]
    end = next((i for i, depth, _ in headings if i > start and depth <= level), len(lines))
    result = "".join(lines[start:end]).strip()
    if "\n" not in result or not result.split("\n", 1)[1].strip():
        raise WoonError("empty navigation headings are not career contribution evidence")
    return result


class CareerEvidence:
    """Capture/check local evidence only; repository roots are caller configuration."""

    def __init__(self, vault: Path, repositories: dict[str, Path] | None = None) -> None:
        self.vault = vault.resolve()
        self.repositories = repositories or {}

    def capture(self, spec: dict[str, Any]) -> dict[str, Any]:
        canonical_id = str(spec.get("canonical_id", ""))
        heading = str(spec.get("section", "")).strip()
        if (
            not canonical_id
            or canonical_id.startswith("wiki/")
            or canonical_id.endswith(".md")
            or not heading
            or "/" not in canonical_id
        ):
            raise WoonError("career evidence requires canonical_id and an exact section")
        path = wiki_path(self.vault, f"wiki/{canonical_id}.md")
        data = path.read_bytes()
        text = data.decode("utf-8").replace("\r\n", "\n")
        metadata: dict[str, Any] = {}
        if text.startswith("---\n") and "\n---\n" in text[4:]:
            raw, text = text[4:].split("\n---\n", 1)
            try:
                loaded = yaml.safe_load(raw)
            except yaml.YAMLError as error:
                raise WoonError("career evidence metadata is invalid") from error
            if not isinstance(loaded, dict):
                raise WoonError("career evidence metadata must be an object")
            metadata = loaded
        if metadata.get("canonical_id", canonical_id) != canonical_id:
            raise WoonError("career evidence canonical identity mismatch")
        if metadata.get("entity_kind") == "career-application" or str(
            metadata.get("type", "")
        ).casefold() in {"resume", "cv", "cover-letter"}:
            raise WoonError("generated application prose is not career evidence")
        excerpt = section_text(text, heading)
        revision = bytes_digest(data)
        if spec.get("revision") and spec["revision"] != revision:
            raise WoonError("career evidence revision changed; inspect and capture it again")
        raw_code = spec.get("code_refs", [])
        if not isinstance(raw_code, list) or not all(isinstance(x, dict) for x in raw_code):
            raise WoonError("career code_refs must be a list of objects")
        code = [self._code_reference(item) for item in raw_code]
        if len({item["locator"] for item in code}) != len(code):
            raise WoonError("career code evidence contains duplicate locators")
        # A title-only legacy document is still readable; an application never is.
        context = {
            key: value
            for key, value in metadata.items()
            if key not in {"updated", "created", "title", "aliases"}
        }
        if isinstance(context.get("llm_wiki"), dict):
            # A compiler rebuild is not a change in the selected contribution.
            context["llm_wiki"] = {
                key: value for key, value in context["llm_wiki"].items() if key != "build_id"
            }
        return {
            "canonical_id": canonical_id,
            "section": heading,
            "revision": revision,
            "section_sha256": bytes_digest(excerpt.encode()),
            "metadata_sha256": digest(context),
            "code_refs": code,
        }

    def validate(self, reference: dict[str, Any]) -> dict[str, Any]:
        """Validate a caller-reviewed pin, not the truth of its career claim."""
        if not re.fullmatch(r"[0-9a-f]{64}", str(reference.get("revision", ""))):
            raise WoonError("career evidence requires an exact revision SHA-256")
        current = self.capture(reference)
        for field in ("section_sha256", "metadata_sha256"):
            if field in reference and reference[field] != current[field]:
                raise WoonError(f"career evidence {field} mismatch")
        if any(
            item["scope"] == "current" and item["state"] != "same"
            for item in self._checkout_observations(current["code_refs"])
        ):
            raise WoonError("current career code differs from its reviewed commit")
        return current

    def inspect(
        self, reference: dict[str, Any], *, include_excerpt: bool = False
    ) -> dict[str, Any]:
        """Report only changed selected content/context, not unrelated page edits."""
        try:
            current = self.capture(
                {key: value for key, value in reference.items() if key != "revision"}
            )
            changed = [
                key
                for key in ("section_sha256", "metadata_sha256", "code_refs")
                if current[key] != reference.get(key)
            ]
            checkout = self._checkout_observations(current["code_refs"])
            updates = [item for item in checkout if item["state"] != "same"]
            if any(item["scope"] == "current" for item in updates):
                changed.append("current_code")
            result: dict[str, Any] = {
                "state": "changed" if changed else "current",
                "changed_fields": changed,
                "current_revision": current["revision"],
                "code_updates": updates,
                "checkout": checkout,
            }
            if include_excerpt and not changed:
                page = wiki_path(self.vault, f"wiki/{current['canonical_id']}.md")
                data = page.read_bytes()
                if bytes_digest(data) != current["revision"]:
                    return {"state": "changed", "reason": "evidence changed during readback"}
                result["excerpt"] = section_text(
                    data.decode("utf-8").replace("\r\n", "\n"), current["section"]
                )
            return result
        except (WoonError, OSError, UnicodeError) as error:
            return {"state": "unavailable", "reason": str(error)}

    def _code_reference(self, item: dict[str, Any]) -> dict[str, str]:
        locator = str(item.get("locator", ""))
        commit = str(item.get("commit", ""))
        scope = str(item.get("scope", "historical"))
        if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit):
            raise WoonError("career code evidence requires a full commit hash")
        if scope not in {"historical", "current"}:
            raise WoonError("career code scope must be historical or current")
        root, relative = self._code_location(locator)
        result = self._git(root, "show", f"{commit}:{relative}")
        if result.returncode:
            raise WoonError("career code evidence commit/file could not be resolved locally")
        sha256 = bytes_digest(result.stdout)
        if item.get("sha256") and item["sha256"] != sha256:
            raise WoonError("career code evidence hash mismatch")
        return {"locator": locator, "commit": commit, "sha256": sha256, "scope": scope}

    def _code_location(self, locator: str) -> tuple[Path, str]:
        if not locator.startswith("repo://"):
            raise WoonError("career code locator must be a repo:// file reference")
        identifier, _, relative = locator.removeprefix("repo://").partition("/")
        if (
            not identifier
            or not relative
            or PurePosixPath(relative).is_absolute()
            or ".." in PurePosixPath(relative).parts
            or "\\" in relative
            or any(c in locator for c in "\r\n\0")
        ):
            raise WoonError("career code locator escapes its repository")
        if identifier in self.repositories:
            root = self.repositories[identifier].expanduser().resolve()
        else:
            workspace = discover("")
            root = Registry.load(workspace.root).resolve(
                workspace.root, identifier, must_exist=True
            )
        return root, relative

    def _checkout_observations(self, refs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        observations = []
        for ref in refs:
            root, relative = self._code_location(ref["locator"])
            actual = None
            try:
                path = (root / relative).resolve()
                if not path.is_relative_to(root):
                    state = "unsafe"
                elif not path.is_file():
                    state = "missing"
                else:
                    actual = bytes_digest(path.read_bytes())
                    state = "same" if actual == ref["sha256"] else "changed"
            except OSError:
                state = "unavailable"
            try:
                head = self._git(root, "rev-parse", "HEAD")
                head_value = head.stdout.decode().strip() if head.returncode == 0 else None
            except (WoonError, UnicodeError):
                head_value = None
            observations.append(
                {
                    "locator": ref["locator"],
                    "scope": ref.get("scope", "historical"),
                    "state": state,
                    "checkout_sha256": actual,
                    "head": head_value,
                    "historical_commit": ref["commit"],
                }
            )
        return observations

    def _git(self, root: Path, *arguments: str) -> subprocess.CompletedProcess[bytes]:
        try:
            return subprocess.run(
                ["git", "-C", str(root), *arguments],
                capture_output=True,
                check=False,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise WoonError("career code evidence could not be checked locally") from error
