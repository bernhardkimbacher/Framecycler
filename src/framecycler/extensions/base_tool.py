from abc import ABC, abstractmethod
from typing import List, Dict, Any
from PySide6.QtGui import QAction

class BaseTool(ABC):
    def __init__(self, main_window):
        self.main_window = main_window

    @abstractmethod
    def get_name(self) -> str:
        """
        Returns the unique name/label of the custom extension tool.
        """
        pass

    def on_init(self):
        """
        Called when the tool is initialized and loaded into the application.
        """
        pass

    def on_media_loaded(self, source_index: int, file_path: str, metadata: Dict[str, Any]):
        """
        Event callback fired when a media source is loaded into the source list.
        """
        pass

    def on_frame_changed(self, frame_index: int, timecode: str):
        """
        Event callback fired when the playhead position changes.
        """
        pass

    def get_menu_actions(self) -> List[QAction]:
        """
        Optional list of QActions to insert into the main window's toolbar or menu bars.
        """
        return []
