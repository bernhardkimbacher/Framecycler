"""Extension → still-decoder factory registry for packages and builtins."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..decoders.base import BaseDecoder

DecoderFactory = Callable[[str], BaseDecoder]


@dataclass
class DecoderEntry:
    entry_id: str
    package_id: str
    extensions: list[str]
    factory: DecoderFactory
    priority: int = 0


class DecoderRegistry:
    def __init__(self) -> None:
        self._entries: list[DecoderEntry] = []

    def clear(self) -> None:
        self._entries.clear()

    def register(
        self,
        entry_id: str,
        *,
        package_id: str,
        extensions: list[str],
        factory: DecoderFactory,
        priority: int = 0,
    ) -> None:
        exts = []
        for ext in extensions:
            e = ext.lower()
            if not e.startswith("."):
                e = f".{e}"
            exts.append(e)
        if not exts:
            raise ValueError("extensions must be non-empty")
        # Replace same entry_id if re-registered.
        self._entries = [e for e in self._entries if e.entry_id != entry_id]
        self._entries.append(
            DecoderEntry(
                entry_id=entry_id,
                package_id=package_id,
                extensions=exts,
                factory=factory,
                priority=int(priority),
            )
        )

    def unregister_package(self, package_id: str) -> None:
        self._entries = [e for e in self._entries if e.package_id != package_id]

    def unregister_prefix(self, prefix: str) -> None:
        self._entries = [e for e in self._entries if not e.entry_id.startswith(prefix)]

    def resolve(self, extension: str) -> DecoderFactory | None:
        ext = extension.lower()
        if not ext.startswith("."):
            ext = f".{ext}"
        matches = [e for e in self._entries if ext in e.extensions]
        if not matches:
            return None
        matches.sort(key=lambda e: e.priority, reverse=True)
        return matches[0].factory

    def entries(self) -> list[DecoderEntry]:
        return list(self._entries)
