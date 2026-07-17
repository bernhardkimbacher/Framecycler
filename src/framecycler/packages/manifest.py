"""Parse and discover package.toml manifests."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    import tomli as tomllib  # type: ignore

from .paths import package_search_roots

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PackageManifest:
    id: str
    name: str
    version: str
    description: str
    entry: str
    enabled_by_default: bool
    path: Path
    source: str

    @property
    def entry_module(self) -> str:
        module, _, _ = self.entry.partition(":")
        return module

    @property
    def entry_class(self) -> str:
        _, _, cls = self.entry.partition(":")
        return cls


def parse_manifest(package_dir: Path, source: str) -> PackageManifest | None:
    toml_path = package_dir / "package.toml"
    if not toml_path.is_file():
        return None
    try:
        with open(toml_path, "rb") as handle:
            data = tomllib.load(handle)
    except Exception as exc:
        logger.warning("Failed to parse %s: %s", toml_path, exc)
        return None

    pkg_id = str(data.get("id", "")).strip()
    name = str(data.get("name", "")).strip()
    entry = str(data.get("entry", "")).strip()
    if not pkg_id or not name or ":" not in entry:
        logger.warning(
            "Invalid package.toml at %s (need id, name, entry as module:Class)",
            toml_path,
        )
        return None

    return PackageManifest(
        id=pkg_id,
        name=name,
        version=str(data.get("version", "0.0.0")).strip() or "0.0.0",
        description=str(data.get("description", "")).strip(),
        entry=entry,
        enabled_by_default=bool(data.get("enabled_by_default", False)),
        path=package_dir.resolve(),
        source=source,
    )


def discover_packages(config_dir: str | Path | None = None) -> list[PackageManifest]:
    """Scan search roots; first package id wins when duplicates exist."""
    found: list[PackageManifest] = []
    seen_ids: set[str] = set()

    for source, root in package_search_roots(config_dir):
        if not root.is_dir():
            continue
        try:
            children = sorted(root.iterdir(), key=lambda p: p.name.lower())
        except OSError as exc:
            logger.warning("Cannot list package root %s: %s", root, exc)
            continue
        for child in children:
            if not child.is_dir():
                continue
            manifest = parse_manifest(child, source)
            if manifest is None:
                continue
            if manifest.id in seen_ids:
                logger.info(
                    "Skipping duplicate package id %s at %s (already found)",
                    manifest.id,
                    child,
                )
                continue
            seen_ids.add(manifest.id)
            found.append(manifest)

    return found


def is_package_enabled(manifest: PackageManifest, package_enabled: dict[str, bool]) -> bool:
    if manifest.id in package_enabled:
        return bool(package_enabled[manifest.id])
    return manifest.enabled_by_default
