from __future__ import annotations

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

COLORS = {
    "canvas": "#F4F7FB",
    "surface": "#FFFFFF",
    "surface_muted": "#EEF3F8",
    "ink": "#172235",
    "muted": "#66758A",
    "line": "#DCE4EE",
    "primary": "#246BFD",
    "primary_soft": "#EAF1FF",
    "teal": "#168A83",
    "success": "#21875A",
    "warning": "#C67A16",
    "danger": "#D94747",
    "sidebar": "#152033",
    "sidebar_hover": "#22314A",
}


def stylesheet() -> str:
    c = COLORS
    return f"""
    * {{
        font-family: "Segoe UI Variable", "Microsoft YaHei UI", sans-serif;
        font-size: 13px;
        color: {c["ink"]};
    }}
    QMainWindow, QWidget#AppRoot {{ background: {c["canvas"]}; }}
    QWidget#PrimarySidebar {{ background: {c["sidebar"]}; }}
    QWidget#SecondarySidebar {{ background: {c["surface"]}; border-right: 1px solid {c["line"]}; }}
    QWidget#TopRail {{ background: {c["surface"]}; border-bottom: 1px solid {c["line"]}; }}
    QLabel#BrandMark {{ color: white; font-size: 18px; font-weight: 700; }}
    QLabel#BrandCaption {{ color: #94A5BE; font-size: 10px; font-weight: 600; }}
    QLabel#PageTitle {{ font-size: 24px; font-weight: 700; }}
    QLabel#PageCaption {{ color: {c["muted"]}; font-size: 13px; }}
    QLabel#SectionTitle {{ font-size: 16px; font-weight: 700; }}
    QLabel#MetricValue {{ font-family: "Bahnschrift", "Segoe UI Variable"; font-size: 26px; font-weight: 600; }}
    QLabel#Muted {{ color: {c["muted"]}; }}
    QLabel#Mono {{ font-family: "JetBrains Mono", "Cascadia Mono", monospace; }}
    QFrame#Card {{ background: {c["surface"]}; border: 1px solid {c["line"]}; border-radius: 10px; }}
    QFrame#SoftPanel {{ background: {c["surface_muted"]}; border: none; border-radius: 9px; }}
    QPushButton {{
        min-height: 34px; padding: 0 14px; border-radius: 8px;
        border: 1px solid {c["line"]}; background: {c["surface"]}; font-weight: 600;
    }}
    QPushButton:hover {{ border-color: #A9BAD0; background: #F9FBFD; }}
    QPushButton:pressed {{ background: {c["surface_muted"]}; }}
    QPushButton:focus {{ border: 2px solid {c["primary"]}; }}
    QPushButton:disabled {{ color: #9BA8B8; background: #F2F5F8; }}
    QPushButton[primary="true"] {{ color: white; background: {c["primary"]}; border-color: {c["primary"]}; }}
    QPushButton[primary="true"]:hover {{ background: #1759DF; }}
    QPushButton[danger="true"] {{ color: {c["danger"]}; border-color: #F0BABA; background: #FFF7F7; }}
    QPushButton[ghost="true"] {{ border: none; background: transparent; }}
    QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QDateTimeEdit {{
        min-height: 34px; padding: 0 9px; border: 1px solid {c["line"]};
        border-radius: 7px; background: {c["surface"]}; selection-background-color: {c["primary"]};
    }}
    QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus {{ border: 2px solid {c["primary"]}; }}
    QComboBox::drop-down {{ border: none; width: 28px; }}
    QTableView, QTableWidget {{
        background: {c["surface"]}; alternate-background-color: #F8FAFD;
        border: 1px solid {c["line"]}; border-radius: 8px; gridline-color: #E9EEF5;
        selection-background-color: {c["primary_soft"]}; selection-color: {c["ink"]};
    }}
    QHeaderView::section {{
        background: #F4F7FA; color: {c["muted"]}; border: none; border-bottom: 1px solid {c["line"]};
        padding: 9px; font-weight: 700;
    }}
    QTabWidget::pane {{ border: none; }}
    QTabBar::tab {{ padding: 9px 14px; color: {c["muted"]}; border-bottom: 2px solid transparent; }}
    QTabBar::tab:selected {{ color: {c["primary"]}; border-bottom-color: {c["primary"]}; font-weight: 700; }}
    QProgressBar {{ border: none; background: {c["surface_muted"]}; border-radius: 4px; height: 8px; text-align: center; }}
    QProgressBar::chunk {{ background: {c["primary"]}; border-radius: 4px; }}
    QScrollBar:vertical {{ width: 9px; background: transparent; margin: 2px; }}
    QScrollBar::handle:vertical {{ background: #C5D0DE; min-height: 30px; border-radius: 4px; }}
    QToolTip {{ color: {c["ink"]}; background: white; border: 1px solid {c["line"]}; padding: 6px; }}
    """


def apply_theme(app: QApplication) -> None:
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor(COLORS["canvas"]))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(COLORS["ink"]))
    palette.setColor(QPalette.ColorRole.Base, QColor(COLORS["surface"]))
    palette.setColor(QPalette.ColorRole.Text, QColor(COLORS["ink"]))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(COLORS["primary"]))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#FFFFFF"))
    app.setPalette(palette)
    app.setStyleSheet(stylesheet())
