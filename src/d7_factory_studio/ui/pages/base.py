from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from d7_factory_studio.ui.theme import COLORS


class WorkbenchPage(QScrollArea):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.content = QWidget()
        self.layout = QVBoxLayout(self.content)
        self.layout.setContentsMargins(26, 24, 26, 28)
        self.layout.setSpacing(16)
        self.layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.setWidget(self.content)


class FormSection(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.form = QFormLayout(self)
        self.form.setContentsMargins(0, 0, 0, 0)
        self.form.setHorizontalSpacing(18)
        self.form.setVerticalSpacing(12)
        self.form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)

    def add_field(self, label: str, widget: QWidget) -> None:
        title = QLabel(label)
        title.setObjectName("Muted")
        self.form.addRow(title, widget)


class InlineMessage(QFrame):
    def __init__(self, text: str, tone: str = "info", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        palette = {
            "info": (COLORS["primary_soft"], COLORS["primary"]),
            "warning": ("#FFF4E4", COLORS["warning"]),
            "danger": ("#FDEDED", COLORS["danger"]),
            "success": ("#E9F7F0", COLORS["success"]),
        }
        background, foreground = palette[tone]
        self.setStyleSheet(f"background:{background}; border-radius:8px;")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 9, 12, 9)
        self.label = QLabel(text)
        self.label.setWordWrap(True)
        self.label.setStyleSheet(f"color:{foreground}; font-weight:600;")
        layout.addWidget(self.label)

    def set_text(self, text: str) -> None:
        self.label.setText(text)


class LogConsole(QPlainTextEdit):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setReadOnly(True)
        self.setMinimumHeight(160)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setStyleSheet(
            "QPlainTextEdit {font-family:'JetBrains Mono','Cascadia Mono',monospace; "
            "font-size:15px; color:#D6E1F0; background:#172235; border:none; border-radius:8px; padding:10px;}"
        )
