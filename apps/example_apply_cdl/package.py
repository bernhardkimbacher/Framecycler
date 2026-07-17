"""Example package: apply a hardcoded ASC CDL to the viewer pipeline."""

from PySide6.QtGui import QAction

try:
    from src.framecycler.packages.api import Package, PackageContext
except ImportError:
    from framecycler.packages.api import Package, PackageContext


class ExampleApplyCdlPackage(Package):
    def activate(self, ctx: PackageContext) -> None:
        action = QAction("Apply Example CDL", ctx.parent_widget())
        action.triggered.connect(lambda: self._apply_example_cdl(ctx))
        ctx.add_menu_actions([action])

    def _apply_example_cdl(self, ctx: PackageContext) -> None:
        # ---------------------------------------------------------------------------
        # Stub: replace this block with a fetch from ShotGrid / ftrack / studio API.
        # Example:
        #   response = studio_api.get_cdl(shot_id)
        #   slope, offset, power, sat = parse_cdl(response)
        # ---------------------------------------------------------------------------
        slope = (1.15, 1.05, 0.95)
        offset = (0.0, 0.0, 0.0)
        power = (1.0, 1.0, 1.0)
        saturation = 1.1

        ctx.apply_cdl(
            slope=slope,
            offset=offset,
            power=power,
            saturation=saturation,
        )
        ctx.status("Applied example ASC CDL (hardcoded stub).")
        ctx.logger.info(
            "Applied example CDL slope=%s offset=%s power=%s sat=%s",
            slope,
            offset,
            power,
            saturation,
        )
