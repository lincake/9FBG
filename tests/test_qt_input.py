"""Exercise input wheel protection with real Qt widgets and input events."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtCore import QEvent, QObject, QPoint, QPointF, Qt
from PyQt6.QtGui import QWheelEvent
from PyQt6.QtTest import QSignalSpy, QTest
from PyQt6.QtWidgets import (
    QApplication, QComboBox, QDoubleSpinBox, QLineEdit, QScrollArea,
    QSpinBox, QVBoxLayout, QWidget,
)

from ninefbg.qt_input import install_input_wheel_guard


class WheelRecorder(QObject):
    def __init__(self, parent):
        super().__init__(parent)
        self.events = []

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.Wheel:
            self.events.append({
                "pixel": event.pixelDelta(), "angle": event.angleDelta(),
                "position": event.position(), "global": event.globalPosition(),
                "phase": event.phase(), "inverted": event.inverted(),
                "buttons": event.buttons(), "modifiers": event.modifiers(),
            })
        return False


@pytest.fixture(scope="module")
def app():
    instance = QApplication.instance() or QApplication([])
    install_input_wheel_guard(instance)
    return instance


@pytest.fixture
def controls(app):
    scroll = QScrollArea()
    scroll.resize(440, 300)
    scroll.setWidgetResizable(True)
    content = QWidget()
    layout = QVBoxLayout(content)
    integer = QSpinBox()
    integer.setRange(-100, 100)
    integer.setValue(10)
    decimal = QDoubleSpinBox()
    decimal.setRange(-100, 100)
    decimal.setValue(1.25)
    combo = QComboBox()
    combo.addItems(["一", "二", "三"])
    combo.setCurrentIndex(1)
    sink = QLineEdit()
    fields = {"integer": integer, "decimal": decimal, "combo": combo}
    for field in [*fields.values(), sink]:
        layout.addWidget(field)
    spacer = QWidget()
    spacer.setMinimumHeight(1000)
    layout.addWidget(spacer)
    scroll.setWidget(content)
    recorder = WheelRecorder(scroll.viewport())
    scroll.viewport().installEventFilter(recorder)
    scroll.show()
    scroll.activateWindow()
    app.processEvents()
    yield scroll, fields, sink, recorder
    scroll.close()
    scroll.deleteLater()
    app.processEvents()


def wheel(target, *, pixel=False, inverted=False):
    local = target.rect().center()
    event = QWheelEvent(
        QPointF(local), QPointF(target.mapToGlobal(local)),
        QPoint(0, -30) if pixel else QPoint(),
        QPoint() if pixel else QPoint(0, -120),
        Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.ScrollUpdate, inverted,
    )
    QApplication.sendEvent(target, event)
    return event


def value_and_signal(field):
    if isinstance(field, QComboBox):
        return field.currentIndex(), QSignalSpy(field.currentIndexChanged)
    return field.value(), QSignalSpy(field.valueChanged)


def current_value(field):
    return field.currentIndex() if isinstance(field, QComboBox) else field.value()


@pytest.mark.parametrize("kind", ["integer", "decimal", "combo"])
@pytest.mark.parametrize("focused", [False, True])
@pytest.mark.parametrize("pixel", [False, True])
def test_wheel_never_changes_input_and_reaches_parent_scroll(
    app, controls, kind, focused, pixel,
):
    scroll, fields, sink, recorder = controls
    field = fields[kind]
    (field if focused else sink).setFocus()
    app.processEvents()
    assert field.hasFocus() is focused
    before, changes = value_and_signal(field)

    event = wheel(field, pixel=pixel)

    assert current_value(field) == before
    assert len(changes) == 0
    assert len(recorder.events) == 1
    forwarded = recorder.events[0]
    assert forwarded["pixel"] == event.pixelDelta()
    assert forwarded["angle"] == event.angleDelta()
    assert forwarded["global"] == event.globalPosition()
    assert forwarded["position"] == QPointF(
        scroll.viewport().mapFromGlobal(event.globalPosition().toPoint())
    )
    assert forwarded["phase"] == event.phase()
    assert forwarded["inverted"] == event.inverted()
    if not pixel:
        assert scroll.verticalScrollBar().value() > 0


@pytest.mark.parametrize("kind", ["integer", "decimal", "combo"])
def test_wheel_on_child_editor_is_protected(app, controls, kind):
    scroll, fields, _, recorder = controls
    field = fields[kind]
    if isinstance(field, QComboBox):
        field.setEditable(True)
    editor = field.lineEdit()
    editor.setFocus()
    app.processEvents()
    before, changes = value_and_signal(field)

    wheel(editor)

    assert current_value(field) == before
    assert len(changes) == 0
    assert len(recorder.events) == 1
    assert scroll.verticalScrollBar().value() > 0


@pytest.mark.parametrize("kind", ["integer", "decimal"])
def test_numeric_keyboard_and_step_buttons_still_work(app, controls, kind):
    _, fields, _, _ = controls
    field = fields[kind]
    field.setFocus()
    field.selectAll()
    QTest.keyClicks(field, "12")
    QTest.keyClick(field, Qt.Key.Key_Return)
    assert field.value() == 12
    QTest.keyClick(field, Qt.Key.Key_Up)
    assert field.value() == 12 + field.singleStep()
    before = field.value()
    QTest.mouseClick(
        field, Qt.MouseButton.LeftButton,
        pos=QPoint(field.width() - 5, field.height() // 4),
    )
    assert field.value() == before + field.singleStep()


def test_combo_keyboard_selection_still_works(app, controls):
    _, fields, _, _ = controls
    combo = fields["combo"]
    combo.setFocus()
    QTest.keyClick(combo, Qt.Key.Key_Down)
    assert combo.currentIndex() == 2


def test_install_is_idempotent_and_scrolls_once(app, controls):
    scroll, fields, _, recorder = controls
    install_input_wheel_guard(app)
    install_input_wheel_guard(app)
    viewport = scroll.viewport()
    wheel(viewport)
    expected_scroll = scroll.verticalScrollBar().value()
    assert expected_scroll > 0
    scroll.verticalScrollBar().setValue(0)
    recorder.events.clear()

    wheel(fields["integer"])

    assert len(recorder.events) == 1
    assert scroll.verticalScrollBar().value() == expected_scroll


def test_wheel_preserves_trackpad_and_modifier_metadata(app, controls):
    scroll, fields, _, recorder = controls
    field = fields["decimal"]
    local = field.rect().center()
    event = QWheelEvent(
        QPointF(local), QPointF(field.mapToGlobal(local)),
        QPoint(0, -17), QPoint(0, -120),
        Qt.MouseButton.MiddleButton, Qt.KeyboardModifier.ShiftModifier,
        Qt.ScrollPhase.ScrollEnd, True,
    )
    before = field.value()
    QApplication.sendEvent(field, event)
    assert field.value() == before
    assert len(recorder.events) == 1
    forwarded = recorder.events[0]
    assert forwarded["pixel"] == QPoint(0, -17)
    assert forwarded["angle"] == QPoint(0, -120)
    assert forwarded["phase"] == Qt.ScrollPhase.ScrollEnd
    assert forwarded["inverted"] is True
    assert forwarded["buttons"] == Qt.MouseButton.MiddleButton
    assert forwarded["modifiers"] == Qt.KeyboardModifier.ShiftModifier


def test_open_combo_list_remains_scrollable_and_clickable(app, controls):
    _, fields, _, recorder = controls
    combo = fields["combo"]
    combo.clear()
    combo.addItems([f"选项 {index}" for index in range(80)])
    combo.setMaxVisibleItems(6)
    combo.showPopup()
    app.processEvents()
    view = combo.view()
    list_recorder = WheelRecorder(view.viewport())
    view.viewport().installEventFilter(list_recorder)
    page_scroll_count = len(recorder.events)
    before = view.verticalScrollBar().value()

    wheel(view.viewport())

    assert len(list_recorder.events) == 1
    assert view.verticalScrollBar().value() > before
    assert len(recorder.events) == page_scroll_count
    index = view.indexAt(view.viewport().rect().center())
    assert index.isValid()
    QTest.mouseClick(
        view.viewport(), Qt.MouseButton.LeftButton,
        pos=view.visualRect(index).center(),
    )
    assert combo.currentIndex() == index.row()
    combo.hidePopup()
