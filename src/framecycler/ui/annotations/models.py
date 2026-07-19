"""Annotation shape model (image-normalized coordinates)."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class AnnotationTool(str, Enum):
    SELECT = "select"
    FREEHAND = "freehand"
    LINE = "line"
    ARROW = "arrow"
    RECT = "rect"
    ELLIPSE = "ellipse"
    TEXT = "text"


class AnnotationKind(str, Enum):
    FREEHAND = "freehand"
    LINE = "line"
    ARROW = "arrow"
    RECT = "rect"
    ELLIPSE = "ellipse"
    TEXT = "text"


@dataclass
class AnnotationShape:
    """One drawable stored in image UV space (u,v in 0..1)."""

    kind: AnnotationKind
    points: list[tuple[float, float]] = field(default_factory=list)
    color: str = "#FFCC00"
    thickness: float = 0.004  # fraction of image height
    text: str = ""
    selected: bool = False

    def clone(self) -> "AnnotationShape":
        return deepcopy(self)


def tool_to_kind(tool: AnnotationTool) -> Optional[AnnotationKind]:
    if tool == AnnotationTool.SELECT:
        return None
    try:
        return AnnotationKind(tool.value)
    except ValueError:
        return None


DEFAULT_COLORS = (
    "#FFCC00",
    "#FF3B30",
    "#34C759",
    "#0A84FF",
    "#FFFFFF",
    "#000000",
)
