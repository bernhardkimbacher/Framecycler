"""Dockable Annotations panel: Photoshop-style vertical tool strip."""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QButtonGroup,
    QColorDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..fonts import ui_font
from .icons import tool_icon
from .models import DEFAULT_COLORS, AnnotationTool

_TOOL_STRIP = (
    (AnnotationTool.SELECT, "Select (V)"),
    (AnnotationTool.FREEHAND, "Freehand (B)"),
    (AnnotationTool.LINE, "Line"),
    (AnnotationTool.ARROW, "Arrow"),
    (AnnotationTool.RECT, "Rectangle"),
    (AnnotationTool.ELLIPSE, "Ellipse"),
    (AnnotationTool.TEXT, "Text (T)"),
)


class AnnotationsPanel(QWidget):
    """View → Panels → Annotations tool strip."""

    hide_requested = Signal()
    tool_changed = Signal(object)  # AnnotationTool
    color_changed = Signal(str)
    thickness_changed = Signal(float)
    clear_requested = Signal()
    delete_requested = Signal()

    def __init__(self, main_window=None, parent=None):
        super().__init__(parent)
        self._main_window = main_window
        self._tool = AnnotationTool.SELECT
        self._color = DEFAULT_COLORS[0]
        self._thickness = 0.004

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(8)

        header = QHBoxLayout()
        title = QLabel("Annotations")
        title.setFont(ui_font(11, weight=QFont.Weight.Bold))
        header.addWidget(title)
        header.addStretch()
        self.btn_close = QPushButton("×")
        self.btn_close.setFixedSize(22, 22)
        self.btn_close.setFlat(True)
        self.btn_close.setToolTip("Hide panel")
        self.btn_close.clicked.connect(self.hide_requested.emit)
        header.addWidget(self.btn_close)
        layout.addLayout(header)

        hint = QLabel("Draw on the viewer. Per-shot session memory.")
        hint.setFont(ui_font(9))
        hint.setStyleSheet("color: #889099;")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        body = QHBoxLayout()
        body.setSpacing(10)

        # Vertical icon tool strip (Photoshop-style).
        strip = QVBoxLayout()
        strip.setSpacing(2)
        strip.setContentsMargins(0, 0, 0, 0)
        self._tool_group = QButtonGroup(self)
        self._tool_group.setExclusive(True)
        self._tool_buttons: dict[AnnotationTool, QToolButton] = {}
        for tool, tip in _TOOL_STRIP:
            btn = QToolButton()
            btn.setIcon(tool_icon(tool))
            btn.setIconSize(QSize(22, 22))
            btn.setFixedSize(32, 32)
            btn.setCheckable(True)
            btn.setAutoRaise(True)
            btn.setToolTip(tip)
            btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
            btn.setStyleSheet(
                "QToolButton { border: 1px solid transparent; border-radius: 4px; padding: 3px; }"
                "QToolButton:hover { background: #3a3a3a; }"
                "QToolButton:checked { background: #2f6fed; border-color: #5a8fff; }"
            )
            self._tool_group.addButton(btn)
            self._tool_buttons[tool] = btn
            strip.addWidget(btn, alignment=Qt.AlignmentFlag.AlignHCenter)
            btn.clicked.connect(lambda checked=False, t=tool: self._on_tool(t))
        strip.addStretch(1)
        body.addLayout(strip)

        # Style / actions column
        style_col = QVBoxLayout()
        style_col.setSpacing(6)
        style_label = QLabel("Style")
        style_label.setFont(ui_font(10, weight=QFont.Weight.Bold))
        style_col.addWidget(style_label)

        self.btn_color = QPushButton("Color")
        self.btn_color.setMinimumHeight(28)
        self.btn_color.clicked.connect(self._pick_color)
        style_col.addWidget(self.btn_color)

        swatches = QHBoxLayout()
        swatches.setSpacing(4)
        for hex_color in DEFAULT_COLORS:
            swatch = QToolButton()
            swatch.setFixedSize(18, 18)
            swatch.setStyleSheet(
                f"background-color: {hex_color}; border: 1px solid #555; border-radius: 2px;"
            )
            swatch.setToolTip(hex_color)
            swatch.clicked.connect(lambda checked=False, c=hex_color: self._set_color(c))
            swatches.addWidget(swatch)
        swatches.addStretch()
        style_col.addLayout(swatches)
        self._update_color_button()

        thick_row = QHBoxLayout()
        thick_row.addWidget(QLabel("W"))
        self.slider_thickness = QSlider(Qt.Orientation.Horizontal)
        self.slider_thickness.setRange(1, 50)
        self.slider_thickness.setValue(4)
        self.slider_thickness.valueChanged.connect(self._on_thickness)
        thick_row.addWidget(self.slider_thickness, stretch=1)
        self.lbl_thickness = QLabel("4")
        thick_row.addWidget(self.lbl_thickness)
        style_col.addLayout(thick_row)

        self.btn_delete = QPushButton("Delete")
        self.btn_delete.setToolTip("Delete selected")
        self.btn_delete.clicked.connect(self.delete_requested.emit)
        style_col.addWidget(self.btn_delete)
        self.btn_clear = QPushButton("Clear")
        self.btn_clear.setToolTip("Clear all for this shot")
        self.btn_clear.clicked.connect(self.clear_requested.emit)
        style_col.addWidget(self.btn_clear)
        style_col.addStretch(1)
        body.addLayout(style_col, stretch=1)

        layout.addLayout(body, stretch=1)
        self._tool_buttons[AnnotationTool.SELECT].setChecked(True)

        self.setMinimumWidth(180)
        self.setMaximumWidth(280)

    @property
    def tool(self) -> AnnotationTool:
        return self._tool

    @property
    def color(self) -> str:
        return self._color

    @property
    def thickness(self) -> float:
        return self._thickness

    def _on_tool(self, tool: AnnotationTool) -> None:
        self._tool = tool
        btn = self._tool_buttons.get(tool)
        if btn is not None:
            btn.setChecked(True)
        # Refresh icons so the active tool can stay visually distinct if needed.
        for t, b in self._tool_buttons.items():
            b.setIcon(tool_icon(t, active=t == tool))
        self.tool_changed.emit(tool)

    def _set_color(self, color: str) -> None:
        self._color = color
        self._update_color_button()
        self.color_changed.emit(color)

    def _pick_color(self) -> None:
        initial = QColor(self._color)
        chosen = QColorDialog.getColor(initial, self, "Annotation Color")
        if chosen.isValid():
            self._set_color(chosen.name(QColor.NameFormat.HexRgb).upper())

    def _update_color_button(self) -> None:
        self.btn_color.setStyleSheet(
            f"background-color: {self._color}; color: #111; font-weight: 600;"
        )

    def _on_thickness(self, value: int) -> None:
        # Map 1..50 → ~0.001..0.018 of image height.
        self._thickness = 0.001 + (value / 50.0) * 0.017
        self.lbl_thickness.setText(str(value))
        self.thickness_changed.emit(self._thickness)

    def set_selection_enabled(self, has_selection: bool) -> None:
        self.btn_delete.setEnabled(bool(has_selection))
