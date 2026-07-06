from PySide6.QtGui import QFont, QFontDatabase


def ui_font(
    point_size: int | None = None,
    weight: QFont.Weight = QFont.Weight.Normal,
) -> QFont:
    font = QFontDatabase.systemFont(QFontDatabase.SystemFont.GeneralFont)
    if point_size is not None:
        font.setPointSize(point_size)
    font.setWeight(weight)
    return font


def mono_font(
    point_size: int | None = None,
    weight: QFont.Weight = QFont.Weight.Normal,
) -> QFont:
    font = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
    if point_size is not None:
        font.setPointSize(point_size)
    font.setWeight(weight)
    return font


def ui_family() -> str:
    return QFontDatabase.systemFont(QFontDatabase.SystemFont.GeneralFont).family()


def mono_family() -> str:
    return QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont).family()
