from __future__ import annotations

from pathlib import Path

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
    resources = Path(__file__).resolve().parent.parent / "resources"
    plus_icon = (resources / "spin-plus.svg").as_posix()
    minus_icon = (resources / "spin-minus.svg").as_posix()
    chevron_icon = (resources / "chevron-down.svg").as_posix()
    check_icon = (resources / "check.svg").as_posix()
    return f"""
    * {{
        font-family: "Segoe UI Variable", "Microsoft YaHei UI", sans-serif;
        font-size: 16px;
        color: {c["ink"]};
    }}
    QMainWindow, QWidget#AppRoot {{ background: {c["canvas"]}; }}
    QWidget#PrimarySidebar {{ background: {c["sidebar"]}; }}
    QWidget#SecondarySidebar {{ background: {c["surface"]}; border-right: 1px solid {c["line"]}; }}
    QWidget#TopRail {{ background: {c["surface"]}; border-bottom: 1px solid {c["line"]}; }}
    QLabel#BrandCaption {{ color: #A9B8CC; font-size: 13px; font-weight: 700; }}
    QLabel#PageTitle {{ font-size: 28px; font-weight: 700; }}
    QLabel#PageCaption {{ color: {c["muted"]}; font-size: 16px; }}
    QLabel#SectionTitle {{ font-size: 19px; font-weight: 700; }}
    QLabel#MetricValue {{ font-family: "Bahnschrift", "Segoe UI Variable"; font-size: 30px; font-weight: 600; }}
    QLabel#Muted {{ color: {c["muted"]}; }}
    QLabel#Mono {{ font-family: "JetBrains Mono", "Cascadia Mono", monospace; }}
    QFrame#Card {{ background: {c["surface"]}; border: 1px solid {c["line"]}; border-radius: 10px; }}
    QFrame#SoftPanel {{ background: {c["surface_muted"]}; border: none; border-radius: 9px; }}
    QPushButton {{
        min-height: 42px; padding: 0 16px; border-radius: 9px;
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
    QPushButton[choice="true"] {{
        min-height: 40px; padding: 0 16px; border-radius: 10px;
        color: {c["muted"]}; border: 1px solid {c["line"]}; background: {c["surface"]};
    }}
    QPushButton[choice="true"]:hover {{ color: {c["primary"]}; border-color: #9DBBFF; background: #F7FAFF; }}
    QPushButton[choice="true"]:checked {{
        color: {c["primary"]}; border: 2px solid {c["primary"]}; background: {c["primary_soft"]};
        font-weight: 700;
    }}
    QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QDateTimeEdit {{
        min-height: 42px; padding: 0 11px; border: 1px solid {c["line"]};
        border-radius: 9px; background: {c["surface"]}; selection-background-color: {c["primary"]};
    }}
    QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus {{ border: 2px solid {c["primary"]}; }}
    QComboBox::drop-down {{ border: none; border-left: 1px solid #E6ECF3; width: 38px; }}
    QComboBox::down-arrow {{ image: url("{chevron_icon}"); width: 15px; height: 15px; }}
    QComboBox QAbstractItemView {{
        padding: 5px; outline: none; border: 1px solid #C9D5E4; border-radius: 8px;
        background: {c["surface"]}; selection-background-color: {c["primary"]};
        selection-color: white;
    }}
    QComboBox QAbstractItemView::item {{ min-height: 38px; padding: 0 10px; }}
    QCheckBox {{ spacing: 8px; }}
    QCheckBox::indicator {{
        width: 18px; height: 18px; border-radius: 4px;
        border: 1px solid #B7C4D4; background: white;
    }}
    QCheckBox::indicator:hover {{ border-color: #7FA4F8; background: #F7FAFF; }}
    QCheckBox::indicator:checked {{
        image: url("{check_icon}"); border: 1px solid {c["primary"]}; background: {c["primary"]};
    }}
    QSpinBox, QDoubleSpinBox, QDateTimeEdit {{ padding-right: 38px; }}
    QSpinBox::up-button, QDoubleSpinBox::up-button, QDateTimeEdit::up-button {{
        subcontrol-origin: border; subcontrol-position: top right;
        width: 30px; background: {c["primary_soft"]};
        border: none; border-left: 1px solid #C9D8F2; border-bottom: 1px solid #D7E2F4;
        border-top-right-radius: 8px;
    }}
    QSpinBox::down-button, QDoubleSpinBox::down-button, QDateTimeEdit::down-button {{
        subcontrol-origin: border; subcontrol-position: bottom right;
        width: 30px; background: {c["primary_soft"]};
        border: none; border-left: 1px solid #C9D8F2;
        border-bottom-right-radius: 8px;
    }}
    QSpinBox::up-button:hover, QSpinBox::down-button:hover,
    QDoubleSpinBox::up-button:hover, QDoubleSpinBox::down-button:hover,
    QDateTimeEdit::up-button:hover, QDateTimeEdit::down-button:hover {{ background: #DCE8FF; }}
    QSpinBox::up-arrow, QDoubleSpinBox::up-arrow, QDateTimeEdit::up-arrow {{
        image: url("{plus_icon}"); width: 12px; height: 12px;
    }}
    QSpinBox::down-arrow, QDoubleSpinBox::down-arrow, QDateTimeEdit::down-arrow {{
        image: url("{minus_icon}"); width: 12px; height: 12px;
    }}
    QTableView, QTableWidget {{
        background: {c["surface"]}; alternate-background-color: {c["surface"]};
        border: 1px solid #D5DFEB; border-radius: 8px; gridline-color: transparent;
        selection-background-color: {c["primary_soft"]}; selection-color: {c["ink"]};
        outline: none;
    }}
    QTableView::item, QTableWidget::item {{
        padding: 5px 10px; border: none; border-bottom: 1px solid #E7EDF4;
    }}
    QTableView::item:hover, QTableWidget::item:hover {{ background: #F7FAFE; }}
    QTableView::item:selected, QTableWidget::item:selected {{
        color: {c["ink"]}; background: {c["primary_soft"]}; border-bottom: 1px solid #D5E2FF;
    }}
    QHeaderView::section {{
        background: #F3F6FA; color: #52637A; border: none; border-bottom: 1px solid #D5DFEB;
        padding: 10px 8px; font-weight: 700;
    }}
    QTableCornerButton::section {{ background: #F3F6FA; border: none; border-bottom: 1px solid #D5DFEB; }}
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
