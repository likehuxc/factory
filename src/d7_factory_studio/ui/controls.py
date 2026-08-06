from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFocusEvent, QMouseEvent, QPalette, QWheelEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QAbstractSpinBox,
    QComboBox,
    QDateTimeEdit,
    QDoubleSpinBox,
    QSpinBox,
    QTableWidget,
)


class _WheelAfterFocusMixin:
    """Prevent hover-wheel edits while preserving wheel use after an explicit click."""

    def _configure_wheel_focus(self) -> None:
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._wheel_enabled_after_click = False

    def mousePressEvent(self, event: QMouseEvent) -> None:
        self._wheel_enabled_after_click = True
        super().mousePressEvent(event)

    def focusOutEvent(self, event: QFocusEvent) -> None:
        self._wheel_enabled_after_click = False
        super().focusOutEvent(event)

    def wheelEvent(self, event: QWheelEvent) -> None:
        if self.hasFocus() and self._wheel_enabled_after_click:
            super().wheelEvent(event)
        else:
            event.ignore()


class D7ComboBox(_WheelAfterFocusMixin, QComboBox):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._configure_wheel_focus()


class _D7SpinMixin(_WheelAfterFocusMixin):
    def _configure_spinbox(self) -> None:
        self._configure_wheel_focus()
        self.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.PlusMinus)
        palette = self.palette()
        palette.setColor(QPalette.ColorRole.ButtonText, QColor("#246BFD"))
        self.setPalette(palette)


class D7SpinBox(_D7SpinMixin, QSpinBox):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._configure_spinbox()


class D7DoubleSpinBox(_D7SpinMixin, QDoubleSpinBox):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._configure_spinbox()


class D7DateTimeEdit(_D7SpinMixin, QDateTimeEdit):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._configure_spinbox()


class D7TableWidget(QTableWidget):
    """Consistent dense read-only table; editable pages can opt back into edit triggers."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.setAlternatingRowColors(False)
        self.setShowGrid(False)
        self.setWordWrap(False)
        self.setCornerButtonEnabled(False)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.verticalHeader().setVisible(False)
        self.verticalHeader().setDefaultSectionSize(38)
        self.horizontalHeader().setMinimumHeight(44)
        self.horizontalHeader().setDefaultAlignment(Qt.AlignmentFlag.AlignCenter)
        self.horizontalHeader().setHighlightSections(False)
