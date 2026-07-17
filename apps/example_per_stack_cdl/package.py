"""Example package: assign alternating R/G/B ASC CDL per shot stack."""

from __future__ import annotations

import os

from PySide6.QtGui import QAction

try:
    from src.framecycler.core import otio_model
    from src.framecycler.packages.api import Package, PackageContext
except ImportError:
    from framecycler.core import otio_model
    from framecycler.packages.api import Package, PackageContext

# Alternating per-stack tints (slope RGB). Easy to see when scrubbing shots.
_STACK_TINTS = (
    (1.45, 0.65, 0.65),  # red
    (0.65, 1.45, 0.65),  # green
    (0.65, 0.65, 1.45),  # blue
)


def assign_stack_cdls(ctx: PackageContext) -> int:
    """Write alternating stack CDLs. Returns number of stacks updated."""
    stacks = otio_model.shot_stacks(ctx.session.timeline)
    if not stacks:
        return 0

    for shot_index, stack in enumerate(stacks):
        clips = otio_model.version_clips(stack)
        if clips:
            path = otio_model.media_path_from_clip(clips[0]) or clips[0].name or ""
            filename = os.path.basename(path)
            # ---------------------------------------------------------------------------
            # Demo only: a real studio package would use this filename (or a ShotGrid /
            # ftrack id derived from it) to fetch the correct CDL. We do not use it here.
            # ---------------------------------------------------------------------------
            ctx.logger.info(
                "Stack %s first clip filename (lookup stub): %s",
                shot_index,
                filename,
            )

        slope = _STACK_TINTS[shot_index % len(_STACK_TINTS)]
        ctx.session.set_stack_cdl(
            shot_index,
            slope=slope,
            offset=(0.0, 0.0, 0.0),
            power=(1.0, 1.0, 1.0),
            saturation=1.0,
        )

    ctx.apply_resolved_cdl()
    return len(stacks)


class ExamplePerStackCdlPackage(Package):
    def activate(self, ctx: PackageContext) -> None:
        action = QAction("Apply Example Per-Stack CDLs", ctx.parent_widget())
        action.triggered.connect(lambda: self._on_apply(ctx))
        ctx.add_menu_actions([action])

    def _on_apply(self, ctx: PackageContext) -> None:
        count = assign_stack_cdls(ctx)
        if count <= 0:
            ctx.status("No shot stacks to assign example CDLs.")
            return
        ctx.status(f"Assigned example CDLs to {count} shot stack(s).")
