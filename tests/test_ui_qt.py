"""Qt widgets, driven headlessly with real key and mouse events (QT_QPA_PLATFORM=offscreen)."""
import subprocess
import threading
import time
import tomllib

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel

import tools
from config import settings
from safety.audit import AuditLog
from safety.confirm import Confirmation, ConfirmRequest
from safety.executor import Executor
from ui.capture import TextPromptCapture
from ui.confirm_dialog import ConfirmDialog, QtConfirmer
from ui.frontend import RecordingFrontend
from ui.pill import QtPillFrontend
from ui.tray import TrayController

tools.load_all()


@pytest.fixture(scope="session")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _app(qapp):
    yield
    for w in QApplication.topLevelWidgets():
        w.close()
    QTest.qWait(5)


def wait_for(cond, timeout=4.0):
    # processEvents + time.sleep (not QTest.qWait): qWait keeps the GIL, which would starve the worker threads under test
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        QApplication.processEvents()
        if cond():
            return True
        time.sleep(0.005)
    return cond()


def req(tier="yellow", detail="Create file: C:\\x.txt", **kw):
    return ConfirmRequest(tool=kw.pop("tool", "t"), tier=tier, title=kw.pop("title", "Do the thing?"), detail=detail,
                          requires_non_voice=(tier == "red"), **kw)


def dialog(request, guard=0):
    d = ConfirmDialog(request, input_guard_ms=guard)
    d.present()
    QTest.qWait(20)
    return d


def click(btn):
    QTest.mouseClick(btn, Qt.LeftButton)


# ---- the dialog itself ---------------------------------------------------------------------------------------------------
def test_dialog_shows_the_exact_detail_as_plain_text():
    nasty = "Remove-Item <b>C:\\x</b> & echo <a href='http://evil'>click</a>\nsecond line"
    d = dialog(req("red", detail=nasty, warnings=["touches a protected location"]))
    assert d.detail_view.toPlainText() == nasty and d.detail_view.isReadOnly()
    for label in d.findChildren(QLabel):
        assert label.textFormat() == Qt.PlainText, label.text()
    assert d.badge.text() == "RED" and any("protected location" in w.text() for w in d.warning_labels)


def test_yellow_enter_approves_and_esc_denies():
    d = dialog(req("yellow"))
    QTest.keyClick(d, Qt.Key_Return)
    assert d.result_value == Confirmation(True, "dialog") and not d.isVisible()
    d = dialog(req("yellow"))
    QTest.keyClick(d, Qt.Key_Escape)
    assert d.result_value.approved is False and d.result_value.reason == "declined" and d.result_value.channel == "dialog"


def test_red_enter_does_not_approve_but_a_click_does():
    d = dialog(req("red"))
    assert d.deny_btn.isDefault() and not d.approve_btn.isDefault()
    QTest.keyClick(d, Qt.Key_Return)                       # Enter = the safe default
    assert d.result_value.approved is False
    d = dialog(req("red"))
    click(d.approve_btn)
    assert d.result_value == Confirmation(True, "dialog")
    d = dialog(req("red"))
    QTest.keyClick(d, Qt.Key_Escape)
    assert d.result_value.approved is False


def test_red_can_be_approved_from_the_keyboard_only_by_focusing_the_button():
    d = dialog(req("red"))
    d.approve_btn.setFocus()
    QTest.keyClick(d.approve_btn, Qt.Key_Space)             # Tab to Approve + Space is a deliberate act
    assert d.result_value == Confirmation(True, "dialog")


def test_input_guard_blocks_stray_keystrokes_then_allows_answers():
    d = dialog(req("yellow"), guard=300)
    assert not d.approve_btn.isEnabled() and not d.deny_btn.isEnabled()
    QTest.keyClick(d, Qt.Key_Return)                        # a keystroke meant for another window
    click(d.approve_btn)
    assert d.result_value is None and d.isVisible()
    assert wait_for(lambda: d.approve_btn.isEnabled())
    click(d.approve_btn)
    assert d.result_value.approved


def test_esc_still_denies_during_the_guard():
    d = dialog(req("red"), guard=500)
    QTest.keyClick(d, Qt.Key_Escape)
    assert d.result_value.approved is False


def test_typed_confirm_needs_the_exact_word():
    d = dialog(req("red", kind="typed", typed_word="CONFIRM", step="catastrophic", timeout=60))
    assert not d.approve_btn.isEnabled() and d.typed_edit is not None
    for wrong in ("confirm", "CONFIRM ", "CONFIR", "yes"):
        d.typed_edit.setText(wrong)
        assert not d.approve_btn.isEnabled(), wrong
    QTest.keyClick(d, Qt.Key_Return)                        # Enter with nothing valid typed does nothing
    assert d.result_value is None
    d.typed_edit.setText("CONFIRM")
    assert d.approve_btn.isEnabled()
    QTest.keyClick(d.typed_edit, Qt.Key_Return)
    assert d.result_value == Confirmation(True, "dialog")


def test_typed_confirm_can_be_approved_by_clicking_once_the_word_is_typed():
    d = dialog(req("red", kind="typed", typed_word="CONFIRM"))
    QTest.keyClicks(d.typed_edit, "CONFIRM")
    click(d.approve_btn)
    assert d.result_value.approved


def test_no_answer_counts_as_no():
    d = dialog(req("red", timeout=1))
    assert wait_for(lambda: d.result_value is not None, 4)
    assert d.result_value == Confirmation(False, "none", "timeout")


def test_closing_the_window_denies():
    d = dialog(req("yellow"))
    d.close()
    assert d.result_value.approved is False


def test_elevation_and_catastrophic_steps_are_labelled():
    assert "administrator" in dialog(req("red", step="elevation", title="Run this as administrator?")).step_label.text().lower()
    assert "destructive" in dialog(req("red", step="catastrophic")).step_label.text().lower()


# ---- QtConfirmer: from a worker thread ---------------------------------------------------------------------------------
def ask_in_thread(confirmer, request):
    box = {}
    t = threading.Thread(target=lambda: box.update(r=confirmer.confirm(request)))
    t.start()
    assert wait_for(lambda: confirmer.active_dialog is not None or "r" in box)
    return t, box


def test_confirmer_blocks_the_worker_until_the_user_answers():
    c = QtConfirmer(input_guard_ms=0)
    t, box = ask_in_thread(c, req("yellow"))
    assert t.is_alive() and "r" not in box
    click(c.active_dialog.approve_btn)
    assert wait_for(lambda: "r" in box)
    t.join(2)
    assert box["r"] == Confirmation(True, "dialog") and c.active_dialog is None


def test_confirmer_deny_and_timeout_from_a_worker_thread():
    c = QtConfirmer(input_guard_ms=0)
    t, box = ask_in_thread(c, req("red"))
    QTest.keyClick(c.active_dialog, Qt.Key_Escape)
    assert wait_for(lambda: "r" in box) and box["r"].approved is False
    t, box = ask_in_thread(c, req("red", timeout=1))
    assert wait_for(lambda: "r" in box, 5) and box["r"].reason == "timeout"


def test_confirmer_on_the_gui_thread_uses_a_nested_loop():
    c = QtConfirmer(input_guard_ms=0)
    from PySide6.QtCore import QTimer

    QTimer.singleShot(150, lambda: click(QApplication.activeModalWidget().approve_btn))
    assert c.confirm(req("yellow")) == Confirmation(True, "dialog")


def test_confirmer_announces_through_the_notify_hook():
    seen = []
    c = QtConfirmer(input_guard_ms=0, notify=lambda title, msg: seen.append((title, msg)))
    t, box = ask_in_thread(c, req("yellow", title="Install VLC?"))
    c.active_dialog.reject()
    t.join(3)
    assert seen == [("Vex needs your approval", "Install VLC?")]


# ---- plugged into the real Executor -----------------------------------------------------------------------------------
@pytest.fixture
def world(tmp_path, monkeypatch):
    safe = tmp_path / "safe"
    safe.mkdir()
    monkeypatch.setitem(settings["safety"], "extra_safe_folders", [str(safe)])
    trashed = []
    monkeypatch.setattr("tools.files.send2trash", trashed.append)
    ran = []
    monkeypatch.setattr("tools.shell.subprocess.run", lambda cmd, **kw: ran.append(cmd) or subprocess.CompletedProcess(cmd, 0, "ran", ""))
    confirmer = QtConfirmer(input_guard_ms=0)
    executor = Executor(confirmer, AuditLog(tmp_path / "logs" / "actions.jsonl"), None)
    return safe, confirmer, executor, trashed, ran


def run_tool(executor, name, args):
    box = {}
    t = threading.Thread(target=lambda: box.update(r=executor.run(name, args)))
    t.start()
    return t, box


def test_executor_with_native_dialogs_delete_flow(world):
    safe, confirmer, executor, trashed, ran = world
    f = safe / "precious.txt"
    f.write_text("x")
    t, box = run_tool(executor, "delete_file", {"path": str(f)})
    assert wait_for(lambda: confirmer.active_dialog is not None)
    d = confirmer.active_dialog
    assert str(f) in d.detail_view.toPlainText() and d.req.tier == "red"
    QTest.keyClick(d, Qt.Key_Return)                         # Enter on a red dialog = Deny
    assert wait_for(lambda: "r" in box) and not box["r"].ok and trashed == []
    t, box = run_tool(executor, "delete_file", {"path": str(f)})
    assert wait_for(lambda: confirmer.active_dialog is not None)
    click(confirmer.active_dialog.approve_btn)
    assert wait_for(lambda: "r" in box) and box["r"].ok and trashed == [str(f)]


def test_executor_with_native_dialogs_catastrophic_command_flow(world):
    safe, confirmer, executor, trashed, ran = world
    executor.elevation.begin_turn("run it as administrator")          # the user asked for admin rights in this message
    t, box = run_tool(executor, "run_powershell", {"command": "vssadmin delete shadows /all /quiet", "admin": True})
    # 1) the command itself (red, click)
    assert wait_for(lambda: confirmer.active_dialog is not None)
    d = confirmer.active_dialog
    assert "vssadmin delete shadows /all /quiet" in d.detail_view.toPlainText() and d.req.step == "action"
    assert any("shadow" in w.text() for w in d.warning_labels)
    click(d.approve_btn)
    # 2) typed CONFIRM
    assert wait_for(lambda: confirmer.active_dialog is not None and confirmer.active_dialog.req.step == "catastrophic")
    d = confirmer.active_dialog
    assert not d.approve_btn.isEnabled()
    QTest.keyClicks(d.typed_edit, "CONFIRM")
    click(d.approve_btn)
    # 3) administrator rights, separately
    assert wait_for(lambda: confirmer.active_dialog is not None and confirmer.active_dialog.req.step == "elevation")
    QTest.keyClick(confirmer.active_dialog, Qt.Key_Escape)       # decline elevation
    assert wait_for(lambda: "r" in box)
    assert not box["r"].ok and ran == []


def test_catastrophic_command_runs_only_after_every_step_is_approved(world, monkeypatch):
    safe, confirmer, executor, trashed, ran = world
    t, box = run_tool(executor, "run_powershell", {"command": "diskpart /?"})
    assert wait_for(lambda: confirmer.active_dialog is not None)
    click(confirmer.active_dialog.approve_btn)
    assert wait_for(lambda: confirmer.active_dialog is not None and confirmer.active_dialog.req.kind == "typed")
    QTest.keyClicks(confirmer.active_dialog.typed_edit, "CONFIRM")
    click(confirmer.active_dialog.approve_btn)
    assert wait_for(lambda: "r" in box) and box["r"].ok and len(ran) == 1


# ---- pill --------------------------------------------------------------------------------------------------------------------
def test_pill_applies_every_frontend_call_from_any_thread():
    pill = QtPillFrontend(caption_seconds=0.3)
    pill.show()
    assert wait_for(pill.isVisible)
    done = threading.Event()

    def worker():
        pill.set_state("listening")
        pill.show_caption("hello there")
        pill.set_state("speaking")
        pill.set_level(5.0)
        pill.set_state("nonsense")                           # ignored
        done.set()

    threading.Thread(target=worker).start()
    assert wait_for(done.is_set) and wait_for(lambda: pill.state == "speaking" and pill.caption == "hello there")
    pill.set_level(5.0)
    assert wait_for(lambda: pill._target == 1.0 and pill.level > 0.5)         # clamped to 1, smoothed upward
    pill.set_state("idle")
    assert wait_for(lambda: pill.state == "idle" and pill._target == 0.0)
    assert wait_for(lambda: pill.caption == "", 3)                             # caption fades after caption_seconds
    pill.hide()
    assert wait_for(lambda: not pill.isVisible())
    pill.show()
    assert wait_for(pill.isVisible)


def test_pill_paints_every_state_without_error():
    pill = QtPillFrontend()
    pill.show_caption("a fairly long caption " * 12)
    for state in ("idle", "listening", "thinking", "speaking"):
        pill.set_state(state)
        pill.set_level(0.8)
        QTest.qWait(40)
        img = pill.grab().toImage()
        assert not img.isNull() and img.width() == pill.width()


def test_pill_click_listens_and_drag_moves():
    clicks = []
    pill = QtPillFrontend(on_click=clicks.append)
    pill.show()
    wait_for(pill.isVisible)
    QTest.mouseClick(pill, Qt.LeftButton, pos=QPoint(40, 30))
    assert clicks == ["click"]
    start = pill.pos()
    QTest.mousePress(pill, Qt.LeftButton, pos=QPoint(40, 30))
    QTest.mouseMove(pill, QPoint(120, 80))
    QTest.mouseRelease(pill, Qt.LeftButton, pos=QPoint(120, 80))
    assert clicks == ["click"] and pill.pos() != start                      # a drag is not a click


def test_pill_is_a_visual_frontend():
    from ui.frontend import VisualFrontend

    pill = QtPillFrontend()
    for name in ("set_state", "set_level", "show_caption", "show", "hide"):
        assert callable(getattr(pill, name))
    _ = VisualFrontend


# ---- typed capture -------------------------------------------------------------------------------------------------------
def capture_in_thread(cap):
    box = {}
    t = threading.Thread(target=lambda: box.update(r=cap()))
    t.start()
    assert wait_for(lambda: cap.active_dialog is not None)
    return t, box


def test_text_prompt_returns_what_was_typed():
    cap = TextPromptCapture()
    t, box = capture_in_thread(cap)
    QTest.keyClicks(cap.active_dialog.edit, "open notepad")
    QTest.keyClick(cap.active_dialog.edit, Qt.Key_Return)
    assert wait_for(lambda: "r" in box) and box["r"] == "open notepad"


def test_text_prompt_esc_cancel_and_timeout_return_none():
    cap = TextPromptCapture()
    t, box = capture_in_thread(cap)
    QTest.keyClick(cap.active_dialog, Qt.Key_Escape)
    assert wait_for(lambda: "r" in box) and box["r"] is None
    t, box = capture_in_thread(cap)
    cap.cancel()                                             # e.g. the microphone was muted
    assert wait_for(lambda: "r" in box) and box["r"] is None
    cap2 = TextPromptCapture(timeout_s=1)
    t, box = capture_in_thread(cap2)
    assert wait_for(lambda: "r" in box, 4) and box["r"] is None


# ---- tray ----------------------------------------------------------------------------------------------------------------------
class FakeController:
    def __init__(self):
        self.mic_muted = False
        self.calls = []
        self.listeners = []
        self.on_wakeword_toggle = []

    def start_listening(self, source="hotkey", typed=False):
        self.calls.append(("typed" if typed else "listen", source))
        return "started"

    def set_mic_muted(self, muted):
        self.mic_muted = muted
        self.calls.append(("mute", muted))


class FakeExecutor:
    def __init__(self):
        self.dry_run = False

    def set_dry_run(self, v):
        self.dry_run = v


@pytest.fixture
def tray(tmp_path, qapp, monkeypatch):
    cfg = tmp_path / "config.toml"
    cfg.write_text("[wakeword]\nenabled = true   # tray\n", encoding="utf-8")
    monkeypatch.setitem(settings["wakeword"], "enabled", True)
    opened = []
    monkeypatch.setattr("ui.tray.os.startfile", opened.append)
    quits = []
    controller, frontend, executor = FakeController(), RecordingFrontend(), FakeExecutor()
    t = TrayController(qapp, controller, frontend, executor, config_path=cfg, on_quit=lambda: quits.append(1))
    monkeypatch.setattr(qapp, "quit", lambda: quits.append("app.quit"))
    t.cfg, t.opened, t.quits, t.c, t.f, t.x = cfg, opened, quits, controller, frontend, executor
    return t


def test_tray_menu_has_every_requested_item(tray):
    labels = {k: a.text() for k, a in tray.actions.items()}
    assert set(tray.actions) == {"status", "hotkey", "wakeword_status", "listen", "type", "show", "mute", "wakeword", "followup", "autostart", "dry_run", "memory", "logs", "quit"}
    assert labels["listen"] == "Listen now" and "memory" in labels["memory"].lower() and labels["quit"].startswith("Quit")
    assert [tray.actions[k].isCheckable() for k in ("show", "mute", "wakeword", "autostart", "dry_run")] == [True] * 5
    assert tray.actions["show"].isChecked() and tray.actions["wakeword"].isChecked()
    assert not tray.actions["mute"].isChecked() and not tray.actions["dry_run"].isChecked()


def test_tray_listen_now_uses_the_shared_entry_point(tray):
    tray.actions["listen"].trigger()
    assert tray.c.calls == [("listen", "tray")]


def test_tray_type_a_command_uses_the_typed_prompt(tray):
    tray.actions["type"].trigger()
    assert tray.c.calls == [("typed", "tray")]
    assert tray.actions["type"].text() == "Type a command"


def test_tray_show_hide_mute_dry_run(tray):
    tray.actions["show"].trigger()
    tray.actions["show"].trigger()
    assert [op for op, _ in tray.f.calls] == ["hide", "show"]
    tray.actions["mute"].trigger()
    assert tray.c.mic_muted and tray.c.calls[-1] == ("mute", True)
    tray.actions["mute"].trigger()
    assert not tray.c.mic_muted
    tray.actions["dry_run"].trigger()
    assert tray.x.dry_run is True
    tray.actions["dry_run"].trigger()
    assert tray.x.dry_run is False


def test_tray_wake_word_toggle_is_saved_to_config_toml(tray):
    seen = []
    tray.c.on_wakeword_toggle.append(seen.append)
    tray.actions["wakeword"].trigger()
    text = tray.cfg.read_text(encoding="utf-8")
    assert "enabled = false   # tray" in text and tomllib.loads(text)["wakeword"]["enabled"] is False and seen == [False]
    tray.actions["wakeword"].trigger()
    assert tomllib.loads(tray.cfg.read_text(encoding="utf-8"))["wakeword"]["enabled"] is True and seen == [False, True]


def test_tray_open_logs_memory_and_quit(tray):
    from config import ROOT

    tray.actions["logs"].trigger()
    tray.actions["memory"].trigger()
    assert tray.opened == [ROOT / settings["paths"]["logs"], ROOT / settings["paths"]["vault"]]
    tray.actions["quit"].trigger()
    assert tray.quits == [1, "app.quit"]


def test_tray_status_follows_controller_state_from_any_thread(tray):
    assert tray.c.listeners
    threading.Thread(target=lambda: tray.c.listeners[0]("speaking")).start()
    assert wait_for(lambda: tray.status_action.text() == "Vex: Speaking")
    tray.c.listeners[0]("listening")
    assert wait_for(lambda: "Listening" in tray.status_action.text())


def test_tray_hotkey_status_line(tray):
    tray.set_hotkey_status(False, "Win+Alt+V")
    assert "unavailable" in tray.actions["hotkey"].text()
    tray.set_hotkey_status(True, "Win+Alt+V")
    assert tray.actions["hotkey"].text() == "Hotkey Win+Alt+V"


def test_tray_notify_never_raises_without_a_system_tray(tray):
    tray.notify("Title", "message")


def test_app_pretty_combo():
    from ui.app import pretty_combo

    assert pretty_combo("windows+alt+v") == "Win+Alt+V"
