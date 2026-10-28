"""Shared native 9FBG presentation theme; no device or processing behavior."""
from __future__ import annotations

from pathlib import Path

from PyQt6.QtGui import QColor, QFont, QPalette


_ASSETS = Path(__file__).resolve().parents[1] / "assets" / "ui"


def ui_font(size=10, weight=QFont.Weight.Normal):
    """Use the platform's clear UI faces with an explicit Chinese fallback."""
    font = QFont()
    font.setFamilies(["Segoe UI", "Microsoft YaHei UI", "DengXian", "sans-serif"])
    font.setPointSizeF(float(size))
    font.setWeight(weight)
    return font


STYLE = """
QWidget {
    color: #172b4d;
    font-size: 10pt;
    font-weight: 400;
    selection-background-color: #3167e3;
    selection-color: #ffffff;
}
QMainWindow { background: #f4f7fb; }
QFrame#sidebar { background: #ffffff; border: none; border-right: 1px solid #e2e8f1; }
QLabel { background: transparent; }
QLabel#brand { color: #244d9f; font-size: 27pt; font-weight: 700; }
QLabel#subtitle, QLabel#pageSubheading, QLabel[role="muted"] {
    color: #63738a; font-size: 9pt;
}
QLabel#pageHeading { font-size: 19pt; font-weight: 600; color: #172b4d; }
QLabel#pageTitle { font-size: 14pt; font-weight: 600; color: #172b4d; }
QLabel#versionBadge {
    color: #486488; background: #eaf0f8; border: 1px solid #dce5f2;
    border-radius: 8px; padding: 5px 10px; font-size: 9pt;
}
QLabel#notice, QLabel#pulseServiceNotice {
    color: #33577f; background: #eaf1fc; border: 1px solid #d9e5f9;
    border-radius: 10px; padding: 10px 14px;
}
QLabel#errorNotice, QLabel#notice[role="error"] {
    color: #a13c35; background: #fff1ee; border: 1px solid #f2d0ca;
    border-radius: 10px; padding: 10px 14px;
}
QLabel#liveFrameCount { color: #284b7b; font-size: 11pt; font-weight: 600; }
QLabel#liveSummary { color: #65758b; font-size: 9pt; }
QLabel[role="sectionLabel"] { color: #70809a; font-size: 9pt; font-weight: 600; }
QLabel[role="metricValue"] { font-size: 20pt; font-weight: 600; }
QPushButton[role="point"] { padding: 5px 6px; }

QPushButton, QToolButton {
    background: #ffffff; color: #334e73; border: 1px solid #d3ddeb;
    border-radius: 9px; padding: 7px 12px; min-height: 18px;
    font-weight: 500;
}
QPushButton:hover, QToolButton:hover {
    background: #f0f5ff; color: #244fae; border-color: #a4bce9;
}
QPushButton:pressed, QToolButton:pressed {
    background: #e2ebfb; color: #244fae; border-color: #7599df;
}
QPushButton:focus, QToolButton:focus { border: 1px solid #3167e3; }
QPushButton:checked, QToolButton:checked {
    background: #eaf1ff; color: #2455be; border-color: #99b6ed;
}
QPushButton#primary, QPushButton[role="primary"] {
    background: #3167e3; color: #ffffff; border-color: #3167e3; font-weight: 600;
}
QPushButton#primary:hover, QPushButton[role="primary"]:hover {
    background: #285bd0; border-color: #285bd0;
}
QPushButton#primary:pressed, QPushButton[role="primary"]:pressed {
    background: #204fb9; border-color: #204fb9;
}
QPushButton[role="danger"] {
    background: #fff8f7; color: #a93e35; border-color: #e9c7c2;
}
QPushButton[role="danger"]:hover {
    background: #ffede9; color: #963228; border-color: #d6978e;
}
QPushButton[role="danger"]:pressed { background: #f9dcd6; }
QPushButton[role="ghost"] {
    background: transparent; color: #4e6380; border-color: transparent;
}
QPushButton[role="ghost"]:hover { background: #edf2f9; border-color: #d9e3f0; }
QPushButton:disabled, QToolButton:disabled {
    background: #f1f4f8; color: #7c899b; border-color: #e0e6ee;
}
QPushButton#primary:disabled, QPushButton[role="primary"]:disabled {
    background: #dce6fa; color: #6d82ae; border-color: #d2ddf3;
}

QGroupBox {
    background: #ffffff; border: 1px solid #e0e7f1; border-radius: 12px;
    margin-top: 20px; padding: 16px 14px 14px;
}
QGroupBox::title {
    subcontrol-origin: margin; subcontrol-position: top left; left: 15px;
    padding: 0 7px; color: #28415f; font-weight: 600;
}
QGroupBox#spectrumCard { margin-top: 0; padding: 14px 12px 12px; }
QGroupBox#captureSetupCard { margin-top: 0; padding: 10px 14px 14px; }
QPushButton#captureSetupToggle {
    background: transparent; color: #28415f; border: 1px solid transparent;
    text-align: left; font-weight: 600; padding: 4px 2px;
}
QPushButton#captureSetupToggle:hover { background: #f0f5ff; color: #244fae; }
QPushButton#captureSetupToggle:focus { border-color: #3167e3; }
QTabWidget::pane { border: none; background: #f4f7fb; }
QTabWidget::tab-bar { left: 0; }
QTabBar { background: transparent; }
QTabBar::tab {
    color: #5a6c86; background: #eaf0f7; border: 1px solid transparent;
    border-radius: 9px; margin: 2px 6px 10px 0; padding: 9px 18px;
    min-height: 18px; font-weight: 500;
}
QTabBar::tab:hover { color: #2856af; background: #e1eafa; }
QTabBar::tab:selected {
    color: #ffffff; background: #3167e3; border-color: #3167e3; font-weight: 600;
}
QTabBar::tab:disabled { color: #8793a4; background: #edf1f6; }

QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QDateTimeEdit {
    color: #243e60; background: #ffffff; border: 1px solid #d3ddeb;
    border-radius: 8px; padding: 6px 10px; min-height: 18px;
}
QLineEdit:hover, QSpinBox:hover, QDoubleSpinBox:hover, QComboBox:hover,
QDateTimeEdit:hover { border-color: #a8bcda; }
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus,
QDateTimeEdit:focus { border-color: #3167e3; background: #fcfdff; }
QLineEdit:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled,
QComboBox:disabled, QDateTimeEdit:disabled {
    color: #7c899b; background: #f1f4f8; border-color: #e0e6ee;
}
QLineEdit[readOnly="true"] { color: #5b6e87; background: #f6f8fc; }
QComboBox { padding-right: 32px; }
QComboBox::drop-down {
    subcontrol-origin: padding; subcontrol-position: top right; width: 28px;
    border: none; background: transparent;
}
QComboBox::down-arrow { image: url("__ASSETS__/chevron-down.svg"); width: 12px; height: 12px; }
QComboBox QAbstractItemView {
    background: #ffffff; color: #243e60; border: 1px solid #d3ddeb;
    border-radius: 8px; padding: 5px; outline: none;
    selection-background-color: #eaf1ff; selection-color: #2455be;
}
QComboBox QAbstractItemView::item { min-height: 28px; padding: 3px 7px; }
QSpinBox, QDoubleSpinBox { padding-right: 28px; }
QSpinBox::up-button, QDoubleSpinBox::up-button,
QSpinBox::down-button, QDoubleSpinBox::down-button {
    subcontrol-origin: border; width: 24px; border: none; background: transparent;
}
QSpinBox::up-button, QDoubleSpinBox::up-button { subcontrol-position: top right; }
QSpinBox::down-button, QDoubleSpinBox::down-button { subcontrol-position: bottom right; }
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {
    image: url("__ASSETS__/chevron-up.svg"); width: 9px; height: 9px;
}
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {
    image: url("__ASSETS__/chevron-down.svg"); width: 9px; height: 9px;
}
QSpinBox::up-button:hover, QDoubleSpinBox::up-button:hover,
QSpinBox::down-button:hover, QDoubleSpinBox::down-button:hover { background: #edf3fc; }
QTextEdit, QPlainTextEdit {
    color: #334e73; background: #ffffff; border: 1px solid #d3ddeb;
    border-radius: 9px; padding: 8px; selection-background-color: #3167e3;
}
QTextEdit:focus, QPlainTextEdit:focus { border-color: #3167e3; }
QCheckBox, QRadioButton { spacing: 8px; color: #334e73; }
QCheckBox:disabled, QRadioButton:disabled { color: #7c899b; }
QCheckBox::indicator {
    width: 16px; height: 16px; border: 1px solid #b6c5d9;
    border-radius: 4px; background: #ffffff;
}
QCheckBox::indicator:hover { border-color: #3167e3; background: #f4f7ff; }
QCheckBox::indicator:checked {
    image: url("__ASSETS__/check.svg"); background: #3167e3; border-color: #3167e3;
}
QCheckBox::indicator:disabled { background: #edf1f6; border-color: #d3dce8; }
QCheckBox::indicator:checked:disabled {
    image: url("__ASSETS__/check.svg"); background: #9baed2; border-color: #9baed2;
}
QRadioButton::indicator {
    width: 16px; height: 16px; border-radius: 8px;
    border: 1px solid #b6c5d9; background: #ffffff;
}
QRadioButton::indicator:checked { border: 5px solid #3167e3; background: #ffffff; }
QRadioButton::indicator:disabled { border-color: #c7d2e1; background: #edf1f6; }

QTableWidget, QTableView, QListView, QTreeView {
    color: #334e73; background: #ffffff; alternate-background-color: #f6f8fc;
    border: 1px solid #e0e7f1; border-radius: 9px;
    gridline-color: #edf1f7; selection-background-color: #eaf1ff;
    selection-color: #244fae; outline: none;
}
QTableView::item { padding: 6px; border: none; }
QTableView::item:selected { background: #eaf1ff; color: #244fae; }
QHeaderView::section {
    color: #526984; background: #f1f5fb; padding: 9px 10px;
    border: none; border-bottom: 1px solid #dfe7f2; font-size: 9pt; font-weight: 600;
}
QTableCornerButton::section { background: #f1f5fb; border: none; }
QProgressBar {
    color: #4b6382; background: #eaf0f8; border: none; border-radius: 7px;
    min-height: 15px; text-align: center; font-size: 9pt;
}
QProgressBar::chunk { background: #76a1f3; border-radius: 7px; }
QScrollArea { border: none; background: transparent; }
QScrollArea > QWidget > QWidget { background: #f4f7fb; }
QScrollBar:vertical { width: 10px; margin: 2px 0; background: transparent; }
QScrollBar:horizontal { height: 10px; margin: 0 2px; background: transparent; }
QScrollBar::handle:vertical, QScrollBar::handle:horizontal {
    background: #c5d0df; border: 2px solid #f4f7fb; border-radius: 5px;
}
QScrollBar::handle:vertical { min-height: 30px; }
QScrollBar::handle:horizontal { min-width: 30px; }
QScrollBar::handle:hover { background: #9eafc6; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; background: transparent; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }
QSplitter::handle { background: #e1e8f2; }
QStatusBar { color: #6a7a90; background: #f4f7fb; font-size: 9pt; border-top: 1px solid #e4eaf3; }
QStatusBar::item { border: none; }
QToolTip {
    color: #ffffff; background: #243b5c; border: 1px solid #243b5c;
    border-radius: 6px; padding: 6px 9px;
}
QMenu {
    color: #334e73; background: #ffffff; border: 1px solid #d9e3f0;
    border-radius: 8px; padding: 5px;
}
QMenu::item { padding: 7px 24px 7px 10px; border-radius: 5px; }
QMenu::item:selected { color: #2455be; background: #eaf1ff; }
QMenu::separator { height: 1px; background: #e7edf5; margin: 5px 8px; }
QLabel#pulsePhase { color: #284b7b; font-size: 11pt; font-weight: 600; }
QLabel#pulseReason, QLabel#pulseTiming, QLabel#pulseFilterHelp,
QLabel#pulseChartNote, QLabel#pulseWeightSummary { color: #63738a; font-size: 9pt; }
QLabel#pulseBpm, QLabel#pulseRate, QLabel#pulseValue, QLabel#pulseQuality {
    color: #284b7b; background: #ffffff; border: 1px solid #e0e7f1;
    border-radius: 10px; padding: 12px 14px; font-size: 11pt; font-weight: 600;
}
""".replace("__ASSETS__", _ASSETS.as_posix())


def apply_theme(app):
    """Apply palette as well as QSS so scroll viewports share the canvas tone."""
    app.setStyle("Fusion")
    app.setFont(ui_font())
    palette = QPalette()
    for role, color in (
        (QPalette.ColorRole.Window, "#f4f7fb"),
        (QPalette.ColorRole.WindowText, "#172b4d"),
        (QPalette.ColorRole.Base, "#ffffff"),
        (QPalette.ColorRole.AlternateBase, "#f6f8fc"),
        (QPalette.ColorRole.Text, "#243e60"),
        (QPalette.ColorRole.Button, "#ffffff"),
        (QPalette.ColorRole.ButtonText, "#334e73"),
        (QPalette.ColorRole.Highlight, "#3167e3"),
        (QPalette.ColorRole.HighlightedText, "#ffffff"),
        (QPalette.ColorRole.ToolTipBase, "#243b5c"),
        (QPalette.ColorRole.ToolTipText, "#ffffff"),
        (QPalette.ColorRole.PlaceholderText, "#77879d"),
    ):
        palette.setColor(role, QColor(color))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, QColor("#7c899b"))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, QColor("#7c899b"))
    app.setPalette(palette)
    app.setStyleSheet(STYLE)
