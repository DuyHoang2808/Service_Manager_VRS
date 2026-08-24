"""Stylesheet Qt dung chung cho toan bo cua so."""

from __future__ import annotations

STYLESHEET = """
QMainWindow, QWidget { background: #0f172a; color: #e2e8f0;
    font-family: 'Segoe UI'; font-size: 13px; }
QScrollArea#cardScroll { border: none; background: transparent; }
/* QLabel ke thua QFrame nen dinh luon vien cua rule QFrame ben duoi -> reset truoc */
QLabel, QCheckBox { border: none; background: transparent; }
QFrame#card { background: #1e293b; border: 1px solid #334155; border-radius: 10px; }
QFrame#card:hover { border-color: #475569; }
QLabel#cardTitle { color: #f1f5f9; }
QLabel#statusText { color: #cbd5e1; }
QLabel#metaText { color: #94a3b8; font-size: 12px; }
QLabel#summary { color: #94a3b8; font-size: 14px; }
QLabel#badge { color: #93c5fd; background: #1e3a5f; border-radius: 4px;
    padding: 2px 8px; font-size: 11px; font-weight: bold; }
QPushButton { background: #334155; color: #e2e8f0; border: none;
    border-radius: 6px; padding: 7px 14px; font-weight: 600; }
QPushButton:hover { background: #475569; }
QPushButton#primaryBtn, QPushButton[variant="start"] { background: #16a34a; color: white; }
QPushButton#primaryBtn:hover, QPushButton[variant="start"]:hover { background: #22c55e; }
QPushButton[variant="stop"] { background: #dc2626; color: white; }
QPushButton[variant="stop"]:hover { background: #ef4444; }
QPushButton:disabled { background: #1e293b; color: #64748b; }
QPlainTextEdit#logView { background: #0b1120; color: #cbd5e1;
    border: 1px solid #1e293b; border-radius: 8px; padding: 8px;
    selection-background-color: #1d4ed8; }
QLineEdit { background: #0b1120; border: 1px solid #334155; border-radius: 6px;
    padding: 6px 8px; color: #e2e8f0; }
QLineEdit:focus { border-color: #3b82f6; }
QCheckBox { color: #cbd5e1; }
QTabWidget::pane { border: 1px solid #1e293b; border-radius: 8px; background: #0f172a; }
QTabBar::tab { background: #1e293b; color: #94a3b8; padding: 8px 16px;
    border-top-left-radius: 6px; border-top-right-radius: 6px; margin-right: 2px; }
QTabBar::tab:selected { background: #334155; color: #f1f5f9; }
QSplitter::handle { background: #1e293b; width: 3px; }
QScrollBar:vertical { background: #0f172a; width: 10px; }
QScrollBar::handle:vertical { background: #334155; border-radius: 5px; min-height: 24px; }
QScrollBar::add-line, QScrollBar::sub-line { height: 0; }
"""
