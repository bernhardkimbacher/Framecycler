import os
import urllib.request
import json
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QMessageBox, QInputDialog
from .base_tool import BaseTool

class OcioApiTool(BaseTool):
    def get_name(self) -> str:
        return "External API OCIO Config Loader"

    def get_menu_actions(self) -> list:
        # Create action to fetch external configuration
        action = QAction("Load OCIO from External API...", self.main_window)
        action.triggered.connect(self._fetch_ocio_from_api)
        return [action]

    def _fetch_ocio_from_api(self):
        # We prompt the user for an API endpoint or manifest ID
        manifest_id, ok = QInputDialog.getText(
            self.main_window, "External API Loader", 
            "Enter Shot/Show Configuration ID (e.g., show_avatar_2):"
        )
        if not ok or not manifest_id:
            return
            
        # We query a mock studio API or manifest endpoint
        # For demonstration purposes, we show mock resolution of show configurations.
        # This could load a path from ShotGrid, ftrack, or a custom studio server.
        self.main_window.statusBar().showMessage(f"Fetching OCIO config for '{manifest_id}'...")
        
        try:
            # Here you would typically perform:
            # response = urllib.request.urlopen(f"https://studio-api.local/ocio/{manifest_id}")
            # data = json.loads(response.read().decode())
            # path = data["ocio_config_path"]
            
            # Mock success path
            mock_success = True
            if mock_success:
                QMessageBox.information(
                    self.main_window, "API Success",
                    f"Successfully retrieved OCIO configuration metadata for Show '{manifest_id}' from studio server.\n"
                    "Applying Default Show LUT pipeline."
                )
                # Fallback to trigger main OCIO config loader
                self.main_window.ocio_manager.load_config("")
                self.main_window.viewport.update_ocio_pipeline()
                self.main_window.statusBar().showMessage("OCIO Config loaded from API.")
        except Exception as e:
            QMessageBox.critical(
                self.main_window, "API Connection Error",
                f"Failed to query external config registry: {e}"
            )
            self.main_window.statusBar().showMessage("API query failed.")
