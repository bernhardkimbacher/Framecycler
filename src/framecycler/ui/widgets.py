from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFrame,
    QListView,
    QStyleFactory,
)

MIN_VISIBLE_ROWS = 10
FALLBACK_ROW_HEIGHT = 22

# Qt's native macOS (Aqua) style renders combo box popups like an NSPopUpButton
# menu: it tries to align the currently selected item under the widget and
# shows scroll-arrow affordances whenever that alignment pushes content
# off-screen, regardless of how many items there actually are. Forcing the
# Fusion style makes Qt draw the popup itself (a plain, top-anchored list)
# so our sizing and scroll locking can take effect.
_FUSION_STYLE = QStyleFactory.create("Fusion")


class WideComboBox(QComboBox):
    """Combo box with a wide popup list suited to long OCIO / layer names."""

    def __init__(self, parent=None, min_popup_width: int = 360, max_visible: int = 20):
        super().__init__(parent)
        self._min_popup_width = min_popup_width
        self._popup_width = min_popup_width
        self.setMaxVisibleItems(max(max_visible, MIN_VISIBLE_ROWS))
        self.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.setMinimumContentsLength(14)

        if _FUSION_STYLE is not None:
            self.setStyle(_FUSION_STYLE)

        view = QListView(self)
        view.setUniformItemSizes(True)
        view.setSpacing(0)
        view.setFrameShape(QFrame.Shape.NoFrame)
        view.setViewportMargins(0, 0, 0, 0)
        view.setContentsMargins(0, 0, 0, 0)
        if _FUSION_STYLE is not None:
            view.setStyle(_FUSION_STYLE)
        self.setView(view)

    def refresh_popup_geometry(self):
        if self.count() == 0:
            return

        fm = self.fontMetrics()
        content_width = self._min_popup_width
        for i in range(self.count()):
            content_width = max(content_width, fm.horizontalAdvance(self.itemText(i)) + 48)

        self._popup_width = min(content_width, 720)
        self.view().setMinimumWidth(self._popup_width)
        self.setMinimumWidth(min(self._popup_width, 280))

    def _popup_row_count(self) -> int:
        count = self.count()
        if count <= MIN_VISIBLE_ROWS:
            return max(count, 1)
        return MIN_VISIBLE_ROWS

    def _measure_row_height(self, view: QListView) -> int:
        if self.count() == 0:
            return FALLBACK_ROW_HEIGHT

        measured = max(view.sizeHintForRow(i) for i in range(self.count()))
        if measured <= 0:
            measured = view.fontMetrics().height() + 2
        return max(measured, FALLBACK_ROW_HEIGHT)

    def _apply_popup_geometry(self):
        view = self.view()
        if self.count() == 0:
            return

        row_height = self._measure_row_height(view)
        rows = self._popup_row_count()
        fits_without_scroll = self.count() <= MIN_VISIBLE_ROWS
        if fits_without_scroll:
            # Extra couple of pixels avoids retina rounding clipping the last row.
            list_height = self.count() * row_height + 2
        else:
            list_height = rows * row_height

        if fits_without_scroll:
            view.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        else:
            view.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)

        view.setFixedSize(self._popup_width, list_height)

        scrollbar = view.verticalScrollBar()
        if scrollbar is not None:
            if fits_without_scroll:
                # Prevent Qt from scrolling on hover by removing scroll range.
                scrollbar.setRange(0, 0)
                scrollbar.setValue(0)
            else:
                max_scroll = max(0, (self.count() - rows) * row_height)
                scrollbar.setRange(0, max_scroll)
                scrollbar.setValue(min(scrollbar.value(), max_scroll))

        view.scrollTo(
            view.model().index(0, 0),
            QAbstractItemView.ScrollHint.PositionAtTop,
        )

        container = view.parentWidget()
        if container is not None:
            container.setFrameShape(QFrame.Shape.NoFrame)
            container.setContentsMargins(0, 0, 0, 0)
            layout = container.layout()
            if layout is not None:
                layout.setContentsMargins(0, 0, 0, 0)
                layout.setSpacing(0)
            origin = self.mapToGlobal(self.rect().bottomLeft())
            container.setFixedSize(self._popup_width, list_height)
            container.move(origin)

    def showPopup(self):
        self.refresh_popup_geometry()
        super().showPopup()
        self._apply_popup_geometry()
        # Qt re-layouts the popup once more after showPopup returns; reapply
        # on the next event-loop tick so our height wins over its defaults.
        QTimer.singleShot(0, self._apply_popup_geometry)
