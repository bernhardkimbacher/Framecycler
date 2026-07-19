"""Migrated from extensions/ocio_api_tool.py — mock external OCIO config loader."""

from PySide6.QtGui import QAction
from PySide6.QtWidgets import QInputDialog, QMessageBox

try:
    from framecycler.packages.api import Package, PackageContext
except ImportError:
    from src.framecycler.packages.api import Package, PackageContext


class OcioApiLoaderPackage(Package):
    def activate(self, ctx: PackageContext) -> None:
        action = QAction("Load OCIO from External API...", ctx.parent_widget())
        action.triggered.connect(lambda: self._fetch_ocio_from_api(ctx))
        ctx.add_menu_actions([action])

    def _fetch_ocio_from_api(self, ctx: PackageContext) -> None:
        parent = ctx.parent_widget()
        manifest_id, ok = QInputDialog.getText(
            parent,
            "External API Loader",
            "Enter Shot/Show Configuration ID (e.g., show_avatar_2):",
        )
        if not ok or not manifest_id:
            return

        ctx.status(f"Fetching OCIO config for '{manifest_id}'...")

        try:
            # Here you would typically perform:
            # response = urllib.request.urlopen(f"https://studio-api.local/ocio/{manifest_id}")
            # data = json.loads(response.read().decode())
            # path = data["ocio_config_path"]

            mock_success = True
            if mock_success:
                QMessageBox.information(
                    parent,
                    "API Success",
                    f"Successfully retrieved OCIO configuration metadata for Show "
                    f"'{manifest_id}' from studio server.\n"
                    "Applying Default Show LUT pipeline.",
                )
                ctx.ocio.load_config("")
                ctx.update_ocio_pipeline()
                ctx.status("OCIO Config loaded from API.")
        except Exception as exc:
            QMessageBox.critical(
                parent,
                "API Connection Error",
                f"Failed to query external config registry: {exc}",
            )
            ctx.status("API query failed.")
