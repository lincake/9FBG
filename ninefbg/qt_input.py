"""Keep wheel scrolling from changing acquisition and model settings."""
from __future__ import annotations

from PyQt6.QtCore import QEvent, QObject, QPointF
from PyQt6.QtGui import QWheelEvent
from PyQt6.QtWidgets import (
    QAbstractScrollArea, QAbstractSpinBox, QApplication, QComboBox, QLineEdit,
)


class _InputWheelGuard(QObject):
    def eventFilter(self, watched, event):
        if event.type() != QEvent.Type.Wheel:
            return False
        control = watched
        if isinstance(control, QLineEdit):
            control = control.parentWidget()
        if not isinstance(control, (QAbstractSpinBox, QComboBox)):
            return False

        parent = control.parentWidget()
        while parent is not None and not isinstance(parent, QAbstractScrollArea):
            parent = parent.parentWidget()
        if parent is not None:
            viewport = parent.viewport()
            position = QPointF(viewport.mapFromGlobal(event.globalPosition().toPoint()))
            forwarded = QWheelEvent(
                position, event.globalPosition(), event.pixelDelta(), event.angleDelta(),
                event.buttons(), event.modifiers(), event.phase(), event.inverted(),
                device=event.pointingDevice(),
            )
            QApplication.sendEvent(viewport, forwarded)
            event.setAccepted(forwarded.isAccepted())
        else:
            event.ignore()
        return True


def install_input_wheel_guard(app):
    """Protect existing and later-created fields, even when they have focus."""
    guard = getattr(app, "_ninefbg_input_wheel_guard", None)
    if guard is None:
        guard = _InputWheelGuard(app)
        app.installEventFilter(guard)
        app._ninefbg_input_wheel_guard = guard
    return guard
