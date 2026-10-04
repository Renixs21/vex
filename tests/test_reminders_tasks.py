"""Reminders (one-time, recurring, spoken, list/cancel, restarts), task edit/delete/undo, the daily summary, and Vex announcing on its own."""
import json
import threading
import time
from datetime import datetime, timedelta

import pytest
from helpers import NO, YES, ScriptedConfirmer
from test_ui_core import FakeAgent, FakeCapture, FakeSpeaker

import tools
from safety import undo as undo_mod
from safety.audit import AuditLog
from safety.executor import Executor
from safety.undo import UndoStack
from tools import reminders, tasks
from ui.controller import IDLE, VexController
from ui.frontend import RecordingFrontend

tools.load_all()


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(reminders, "data_path", lambda: tmp_path / "data" / "reminders.json")
    monkeypatch.setattr(tasks, "vault_root", lambda: tmp_path / "vault")
    loud, quiet, spoken = [], [], []
    monkeypatch.setattr(reminders, "notify", lambda t, m: loud.append((t, m)))
    monkeypatch.setattr(reminders, "notify_silent", lambda t, m: quiet.append((t, m)))
    stack = UndoStack(tmp_path / "undo.json")
    undo_mod.configure(stack)
    reminders.stop()
    reminders.set_announcer(None)

    def executor(*answers):
        conf = ScriptedConfirmer(*answers)
        return Executor(conf, AuditLog(tmp_path / "actions.jsonl"), stack), conf

    class W:
        pass

    w = W()
    w.loud, w.quiet, w.spoken, w.executor, w.stack, w.path = loud, quiet, spoken, executor, stack, tmp_path / "data" / "reminders.json"
    w.announce = lambda text: spoken.append(text)
    yield w
    reminders.stop()
    reminders.set_announcer(None)


def saved(w):
    return json.loads(w.path.read_text(encoding="utf-8"))


def edit_saved(w, fn):
    items = saved(w)
    fn(items)
    w.path.write_text(json.dumps(items), encoding="utf-8")


# ---- recurrence parsing -----------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("text,expected", [
    ("every weekday at 9", {"freq": "weekly", "days": [0, 1, 2, 3, 4], "time": "09:00"}),
    ("every weekday at 9am", {"freq": "weekly", "days": [0, 1, 2, 3, 4], "time": "09:00"}),
    ("every Monday at 7pm", {"freq": "weekly", "days": [0], "time": "19:00"}),
    ("every Mon and Thu at 6:30 pm", {"freq": "weekly", "days": [0, 3], "time": "18:30"}),
    ("daily at 8", {"freq": "weekly", "days": list(range(7)), "time": "08:00"}),
    ("every day at noon", {"freq": "weekly", "days": list(range(7)), "time": "12:00"}),
    ("every weekend at 10", {"freq": "weekly", "days": [5, 6], "time": "10:00"}),
    ("every weekday at 17:45", {"freq": "weekly", "days": [0, 1, 2, 3, 4], "time": "17:45"}),
    ("every 2 hours", {"freq": "interval", "minutes": 120}),
    ("every 30 minutes", {"freq": "interval", "minutes": 30}),
    ("every other day at 8am", {"freq": "days", "every": 2, "time": "08:00"}),
    ("every month on the 5th at 10am", {"freq": "monthly", "day": 5, "time": "10:00"}),
])
def test_recurrence_phrases(text, expected):
    assert reminders.is_recurring(text)
    assert reminders.parse_recurrence(text) == expected


@pytest.mark.parametrize("text,asks", [("every sunday", "What time"), ("weekly", "Which days"), ("every 2 minutes", "every 5 minutes"),
                                       ("every month", "Which day of the month"), ("every month on the 31st", "days 1 to 28")])
def test_incomplete_recurrences_become_a_question(text, asks):
    with pytest.raises(ValueError, match=asks):
        reminders.parse_recurrence(text)


def test_one_time_phrases_are_not_recurring():
    assert not any(reminders.is_recurring(t) for t in ("tomorrow 5pm", "in 20 minutes", "friday at 3", "next monday"))


def test_rules_are_described_in_plain_words():
    d = reminders.describe_rule
    assert d(reminders.parse_recurrence("every weekday at 9")) == "every weekday at 9:00 AM"
    assert d(reminders.parse_recurrence("every Mon and Thu at 6:30 pm")) == "every Monday and Thursday at 6:30 PM"
    assert d(reminders.parse_recurrence("every day at noon")) == "every day at 12:00 PM"
    assert d(reminders.parse_recurrence("every 2 hours")) == "every 2 hours"
    assert d(reminders.parse_recurrence("every month on the 2nd at 8am")) == "on the 2nd of every month at 8:00 AM"


def test_next_occurrence_is_in_the_future_on_the_right_day():
    rule = reminders.parse_recurrence("every weekday at 9")
    nxt = reminders.next_occurrence(rule, datetime(2026, 10, 3, 12, 0), datetime(2026, 10, 3, 12, 0))      # a Saturday
    assert nxt == datetime(2026, 10, 5, 9, 0)                                                              # the Monday
    interval = reminders.next_occurrence({"freq": "interval", "minutes": 120}, datetime(2026, 10, 3, 12, 0), datetime(2026, 10, 3, 12, 0))
    assert interval == datetime(2026, 10, 3, 14, 0)                                                        # a full step from now, not now


# ---- set / list / cancel ------------------------------------------------------------------------------------------------------------
def test_set_recurring_reminder_persists_the_rule_and_the_next_time(world):
    ex, _ = world.executor()
    r = ex.run("set_reminder", {"text": "stand up", "when": "every weekday at 9"})
    assert r.ok and "every weekday at 9:00 AM" in r.message and "stand up" in r.message
    item = saved(world)[0]
    assert item["repeat"] == {"freq": "weekly", "days": [0, 1, 2, 3, 4], "time": "09:00"} and not item["done"]
    due = datetime.fromisoformat(item["due"])
    assert due > datetime.now() and due.weekday() < 5 and (due.hour, due.minute) == (9, 0)
    assert reminders._scheduler.get_job(item["id"]) is not None


def test_a_missing_time_or_day_is_asked_not_guessed(world):
    ex, _ = world.executor()
    assert "What time" in ex.run("set_reminder", {"text": "x", "when": "every sunday"}).message
    assert "Which days" in ex.run("set_reminder", {"text": "x", "when": "weekly"}).message
    assert not world.path.exists() or saved(world) == []


def test_list_shows_numbers_in_due_order_with_recurrence(world):
    ex, _ = world.executor()
    ex.run("set_reminder", {"text": "later one", "when": "in 3 hours"})
    ex.run("set_reminder", {"text": "soon one", "when": "in 10 minutes"})
    ex.run("set_reminder", {"text": "stand up", "when": "every weekday at 9"})
    r = ex.run("list_reminders", {})
    lines = r.message.splitlines()
    assert lines[0].startswith("1. soon one") and lines[1].startswith("2. later one")
    assert any("stand up (every weekday at 9:00 AM, next" in l for l in lines)


def test_empty_list(world):
    assert "no reminders" in world.executor()[0].run("list_reminders", {}).message.lower()


def test_cancel_by_number_or_words_and_it_stays_cancelled_after_a_restart(world):
    ex, conf = world.executor()
    ex.run("set_reminder", {"text": "call the dentist", "when": "in 2 hours"})
    ex.run("set_reminder", {"text": "water the plants", "when": "in 3 hours"})
    assert ex.run("cancel_reminder", {"which": "dentist"}).ok
    assert [i["text"] for i in reminders.pending()] == ["water the plants"] and conf.requests == []              # cancelling one is green
    cancelled = next(i for i in saved(world) if i["text"] == "call the dentist")
    assert cancelled["done"] and cancelled["cancelled"] and reminders._scheduler.get_job(cancelled["id"]) is None
    assert ex.run("cancel_reminder", {"which": "1"}).ok and reminders.pending() == []
    reminders.stop()
    reminders.start()                                                                                            # a restart does not bring them back
    assert reminders.pending() == [] and world.loud == [] and world.quiet == []


def test_cancel_asks_for_help_when_unclear(world):
    ex, _ = world.executor()
    ex.run("set_reminder", {"text": "call mom", "when": "in 2 hours"})
    ex.run("set_reminder", {"text": "call mum later", "when": "in 3 hours"})
    amb = ex.run("cancel_reminder", {"which": "call"})
    assert not amb.ok and "Which number" in amb.message and len(reminders.pending()) == 2
    assert "no reminder number 9" in ex.run("cancel_reminder", {"which": "9"}).message
    assert "couldn't find" in ex.run("cancel_reminder", {"which": "zebra"}).message
    assert "Which reminder" in ex.run("cancel_reminder", {}).message


def test_cancel_all_is_yellow_and_asks(world):
    ex, conf = world.executor(NO, YES)
    ex.run("set_reminder", {"text": "a", "when": "in 2 hours"})
    ex.run("set_reminder", {"text": "b", "when": "in 3 hours"})
    assert not ex.run("cancel_reminder", {"all": True}).ok and len(reminders.pending()) == 2
    assert conf.requests[0].tier == "yellow" and conf.requests[0].detail == "Cancel ALL reminders"
    assert ex.run("cancel_reminder", {"all": True}).ok and reminders.pending() == []


# ---- firing: spoken, with a quiet toast ---------------------------------------------------------------------------------------------
def test_a_one_time_reminder_is_spoken_and_the_toast_is_silent(world):
    reminders.set_announcer(world.announce)
    ex, _ = world.executor()
    ex.run("set_reminder", {"text": "water the plants", "when": "in 1 hour"})
    rid = saved(world)[0]["id"]
    reminders._fire(rid)
    assert world.spoken == ["Reminder: water the plants"] and world.quiet == [("Reminder", "water the plants")] and world.loud == []
    assert saved(world)[0]["done"]
    reminders._fire(rid)
    assert len(world.spoken) == 1                                                                           # fires once


def test_without_an_announcer_the_normal_toast_is_used(world):
    ex, _ = world.executor()
    ex.run("set_reminder", {"text": "water the plants", "when": "in 1 hour"})
    reminders._fire(saved(world)[0]["id"])
    assert world.loud == [("Reminder", "water the plants")] and world.quiet == []


def test_if_speaking_fails_the_loud_toast_still_delivers_it(world):
    def broken(text):
        raise RuntimeError("no audio device")

    reminders.set_announcer(broken)
    ex, _ = world.executor()
    ex.run("set_reminder", {"text": "water the plants", "when": "in 1 hour"})
    reminders._fire(saved(world)[0]["id"])
    assert world.loud == [("Reminder", "water the plants")] and world.quiet == []


def test_a_recurring_reminder_fires_and_stays_scheduled_for_the_next_time(world):
    reminders.set_announcer(world.announce)
    ex, _ = world.executor()
    ex.run("set_reminder", {"text": "stand up", "when": "every weekday at 9"})
    rid = saved(world)[0]["id"]
    edit_saved(world, lambda items: items[0].update(due=(datetime.now() - timedelta(minutes=1)).isoformat(timespec="seconds")))
    reminders._fire(rid)
    item = saved(world)[0]
    assert world.spoken == ["Reminder: stand up"] and not item["done"] and item["last_fired"]
    assert datetime.fromisoformat(item["due"]) > datetime.now()                                              # moved on to the next occurrence
    reminders._fire(rid)
    assert len(world.spoken) == 1                                                                           # not twice for the same occurrence
    assert reminders._scheduler.get_job(rid) is not None


# ---- restarts -------------------------------------------------------------------------------------------------------------------------
def wait_for(cond, seconds=5.0):
    end = time.time() + seconds
    while time.time() < end and not cond():
        time.sleep(0.05)
    return cond()


def test_overdue_one_time_reminders_are_announced_as_missed_after_a_restart(world):
    reminders.set_announcer(world.announce)
    world.path.parent.mkdir(parents=True, exist_ok=True)
    world.path.write_text(json.dumps([
        {"id": "late1", "text": "missed one", "due": (datetime.now() - timedelta(hours=1)).isoformat(), "done": False},
        {"id": "done1", "text": "old", "due": (datetime.now() - timedelta(days=1)).isoformat(), "done": True}]), encoding="utf-8")
    reminders.start()
    assert wait_for(lambda: world.spoken)
    assert world.spoken == ["Reminder from earlier: missed one"] and world.quiet == [("Reminder (missed)", "missed one")]


def test_a_recurring_reminder_that_missed_an_occurrence_announces_it_once(world):
    reminders.set_announcer(world.announce)
    now = datetime.now().replace(microsecond=0)
    at = (now - timedelta(minutes=30)).strftime("%H:%M")
    world.path.parent.mkdir(parents=True, exist_ok=True)
    world.path.write_text(json.dumps([{
        "id": "rep1", "text": "stand up", "done": False, "created": (now - timedelta(days=3)).isoformat(),
        "last_fired": (now - timedelta(days=2)).isoformat(), "due": (now - timedelta(minutes=30)).isoformat(),
        "repeat": {"freq": "weekly", "days": list(range(7)), "time": at}}]), encoding="utf-8")
    reminders.start()
    assert wait_for(lambda: world.spoken)
    time.sleep(0.3)
    assert world.spoken == ["Reminder from earlier: stand up"]
    item = saved(world)[0]
    assert not item["done"] and datetime.fromisoformat(item["due"]) > now and item["last_fired"] > (now - timedelta(minutes=1)).isoformat()


def test_a_recurring_reminder_that_missed_nothing_is_just_rescheduled(world):
    reminders.set_announcer(world.announce)
    now = datetime.now().replace(microsecond=0)
    at = (now + timedelta(hours=2)).strftime("%H:%M")
    world.path.parent.mkdir(parents=True, exist_ok=True)
    world.path.write_text(json.dumps([{
        "id": "rep2", "text": "stand up", "done": False, "created": (now - timedelta(days=1)).isoformat(),
        "last_fired": (now - timedelta(minutes=10)).isoformat(), "due": (now + timedelta(hours=2)).isoformat(),
        "repeat": {"freq": "weekly", "days": list(range(7)), "time": at}}]), encoding="utf-8")
    reminders.start()
    time.sleep(0.4)
    assert world.spoken == [] and reminders._scheduler.get_job("rep2") is not None


def test_reminders_survive_stop_and_start(world):
    ex, _ = world.executor()
    ex.run("set_reminder", {"text": "stand up", "when": "every weekday at 9"})
    ex.run("set_reminder", {"text": "later", "when": "in 5 hours"})
    reminders.stop()
    reminders.start()
    assert {j.id for j in reminders._scheduler.get_jobs()} >= {i["id"] for i in saved(world)}
    assert [i["text"] for i in reminders.pending()] in (["later", "stand up"], ["stand up", "later"])


def test_the_sweep_catches_what_a_sleeping_pc_skipped(world):
    reminders.set_announcer(world.announce)
    ex, _ = world.executor()
    ex.run("set_reminder", {"text": "water the plants", "when": "in 1 hour"})
    edit_saved(world, lambda items: items[0].update(due=(datetime.now() - timedelta(minutes=20)).isoformat(timespec="seconds")))
    reminders._sweep()
    assert world.spoken == ["Reminder from earlier: water the plants"]
    reminders._sweep()
    assert len(world.spoken) == 1


# ---- tasks: edit, delete, undo -----------------------------------------------------------------------------------------------------------
def fill(world, *texts):
    ex, conf = world.executor(YES, YES, YES)
    for t in texts:
        assert ex.run("add_task", {"text": t}).ok
    return ex, conf


def lines(world):
    return world.executor()[0].run("list_tasks", {"include_done": True}).message.splitlines()


def test_due_dates_become_real_dates(world):
    ex, _ = world.executor()
    ex.run("add_task", {"text": "pay rent", "due": "tomorrow"})
    ex.run("add_task", {"text": "call mom", "due": "friday at 5pm"})
    ex.run("add_task", {"text": "someday", "due": "when it stops raining"})
    out = lines(world)
    tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    assert f"1. [ ] pay rent (due {tomorrow})" in out and any("call mom (due 20" in l and ":" in l.split("due")[1] for l in out)
    assert "3. [ ] someday (due when it stops raining)" in out                                              # unparseable text is kept


def test_edit_a_task_text_and_due_keeping_its_checkbox(world):
    ex, _ = fill(world, "buy milk", "call the dentist")
    assert ex.run("complete_task", {"task": "1"}).ok
    assert ex.run("edit_task", {"task": "dentist", "text": "call Dr Rao", "due": "2026-12-01"}).ok
    assert lines(world) == ["1. [x] buy milk", "2. [ ] call Dr Rao (due 2026-12-01)"]
    assert ex.run("edit_task", {"task": "2", "due": "none"}).ok and lines(world)[1] == "2. [ ] call Dr Rao"
    assert ex.run("edit_task", {"task": "milk", "text": "buy oat milk"}).ok and lines(world)[0] == "1. [x] buy oat milk"     # still done
    assert "new text or the new due date" in ex.run("edit_task", {"task": "2"}).message


def test_edit_and_delete_need_an_unambiguous_task(world):
    ex, conf = fill(world, "call mom", "call mum later")
    amb = ex.run("edit_task", {"task": "call", "text": "x"})
    assert not amb.ok and "more than one task" in amb.message and "Which number" in amb.message
    assert "no task number 9" in ex.run("delete_task", {"task": "9"}).message
    assert "couldn't find a task" in ex.run("delete_task", {"task": "zebra"}).message
    assert conf.requests == [] and len(lines(world)) == 2


def test_delete_a_task_asks_with_the_exact_text_and_declining_keeps_it(world):
    ex, conf = world.executor(NO, YES)
    ex.run("add_task", {"text": "buy milk"})
    ex.run("add_task", {"text": "call the dentist", "due": "2026-12-01"})
    assert not ex.run("delete_task", {"task": "dentist"}).ok and len(lines(world)) == 2
    req = conf.requests[0]
    assert req.tier == "yellow" and not req.requires_non_voice and req.detail == "Delete task 2: call the dentist"
    assert ex.run("delete_task", {"task": "2"}).ok and lines(world) == ["1. [ ] buy milk"]


def test_undo_puts_a_deleted_task_back_in_its_place(world):
    ex, _ = world.executor(YES)
    for t in ("one", "two", "three"):
        ex.run("add_task", {"text": t})
    assert ex.run("delete_task", {"task": "two"}).ok and lines(world) == ["1. [ ] one", "2. [ ] three"]
    ok, msg, _ = world.stack.undo_last()
    assert ok and "back on your list" in msg and lines(world) == ["1. [ ] one", "2. [ ] two", "3. [ ] three"]


def test_secrets_never_enter_the_task_list_through_edit(world):
    ex, _ = fill(world, "buy milk")
    assert not ex.run("edit_task", {"task": "1", "text": "my password is hunter22secret"}).ok
    assert lines(world) == ["1. [ ] buy milk"]


# ---- what's on today -------------------------------------------------------------------------------------------------------------------
def test_today_summary_combines_reminders_and_tasks(world):
    ex, _ = world.executor()
    today = datetime.now().strftime("%Y-%m-%d")
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    ex.run("add_task", {"text": "send the report", "due": today})
    ex.run("add_task", {"text": "renew passport", "due": yesterday})
    ex.run("add_task", {"text": "clean the desk"})
    ex.run("add_task", {"text": "done already", "due": today})
    ex.run("complete_task", {"task": "done already"})
    soon = (datetime.now() + timedelta(minutes=5)).replace(microsecond=0)
    if soon.date() == datetime.now().date():                                                               # (not within 5 min of midnight)
        ex.run("set_reminder", {"text": "stretch", "when": "in 5 minutes"})
    r = ex.run("today_summary", {})
    assert r.ok and datetime.now().strftime("%A %d %B") in r.message
    assert "Tasks due today: send the report" in r.message and "Overdue tasks: renew passport (was due" in r.message
    assert "1 other open task: clean the desk" in r.message and "done already" not in r.message
    if soon.date() == datetime.now().date():
        assert f"Reminders: {soon:%H:%M} stretch" in r.message
    assert r.data["due_today"] == 1 and r.data["overdue"] == 1


def test_today_summary_when_nothing_is_planned(world):
    r = world.executor()[0].run("today_summary", {})
    assert "Nothing is scheduled and you have no open tasks" in r.message


def test_today_summary_with_only_undated_tasks_says_no_deadlines(world):
    ex, _ = fill(world, "clean the desk")
    assert "No reminders or deadlines today." in ex.run("today_summary", {}).message


def test_the_new_tools_are_in_the_tasks_group_with_the_right_tiers():
    from tools import registry

    cat = {t.name: t.category for t in registry.all_tools()}
    tier = {t.name: t.tier for t in registry.all_tools()}
    for name in ("add_task", "list_tasks", "complete_task", "edit_task", "delete_task", "set_reminder", "list_reminders", "cancel_reminder",
                 "today_summary"):
        assert cat[name] == "tasks", name
    assert tier["delete_task"] == "yellow" and tier["edit_task"] == "green" and tier["cancel_reminder"] == "green"


# ---- Vex announces on its own -----------------------------------------------------------------------------------------------------------
def make_controller(speaker=None, capture=None):
    f = RecordingFrontend()
    sp = speaker or FakeSpeaker()
    c = VexController(FakeAgent(), f, sp, capture or FakeCapture("hello"))
    chimes, starts, ends = [], [], []
    c.chime = lambda: chimes.append(time.time())
    c.on_interaction_start.append(lambda: starts.append(1))
    c.on_interaction_end.append(lambda: ends.append(1))
    return c, f, sp, chimes, starts, ends


def test_an_announcement_chimes_shows_the_words_and_speaks_them():
    c, f, sp, chimes, starts, ends = make_controller()
    assert c.announce("Reminder: stretch") == "started"
    assert c.wait_idle() and sp.spoken == ["Reminder: stretch"] and len(chimes) == 1
    assert ("caption", "Reminder: stretch") in f.calls and f.states == ["speaking", "idle"]
    assert starts == [1] and ends == [1]                                    # the wake word is paused and resumed around it, like any turn
    assert c.state == IDLE


def test_an_announcement_never_interrupts_a_conversation_it_waits_its_turn():
    hold = threading.Event()
    c, f, sp, chimes, starts, ends = make_controller(FakeSpeaker(hold=hold))
    assert c.start_listening() == "started"                                  # Vex is answering: speaking, held
    assert sp.started.wait(3)
    assert c.announce("Reminder: stretch") == "queued"
    assert sp.spoken == ["reply to hello"] and chimes == []                 # not yet: the conversation finishes first
    hold.set()
    deadline = time.time() + 5
    while time.time() < deadline and len(sp.spoken) < 2:
        time.sleep(0.02)
    assert sp.spoken == ["reply to hello", "Reminder: stretch"] and len(chimes) == 1


def test_several_queued_announcements_are_spoken_one_after_another():
    hold = threading.Event()
    c, f, sp, chimes, starts, ends = make_controller(FakeSpeaker(hold=hold))
    c.start_listening()
    assert sp.started.wait(3)
    c.announce("Reminder: one")
    c.announce("Reminder: two")
    hold.set()
    deadline = time.time() + 8
    while time.time() < deadline and len(sp.spoken) < 3:
        time.sleep(0.02)
    assert sp.spoken[-2:] == ["Reminder: one", "Reminder: two"] and len(chimes) == 2


def test_announcing_still_works_without_a_chime_or_when_it_cannot_be_spoken():
    c, f, sp, chimes, starts, ends = make_controller()
    c.chime = None
    assert c.announce("Reminder: a") == "started" and c.wait_idle() and sp.spoken == ["Reminder: a"]

    class Mute(FakeSpeaker):
        def speak(self, text, on_level):
            raise OSError("no output device")

    c2, f2, *_ = make_controller(Mute())
    assert c2.announce("Reminder: b") == "started" and c2.wait_idle()
    assert ("caption", "Reminder: b") in f2.calls and c2.state == IDLE       # the caption still carries it; Vex stays usable


def test_empty_or_shutdown_announcements_do_nothing():
    c, f, sp, *_ = make_controller()
    assert c.announce("   ") == "busy"
    c.shutdown()
    assert c.announce("Reminder: x") == "busy" and sp.spoken == []


def test_the_reminder_chime_is_a_short_quiet_two_note_sound():
    from voice.chime import make_reminder_chime, play_reminder_chime

    s = make_reminder_chime()
    assert s.dtype.name == "float32" and 0.4 < len(s) / 24000 < 0.9 and abs(s).max() < 0.3 and abs(s[0]) < 0.01 and abs(s[-1]) < 0.01
    played = []
    play_reminder_chime(type("SD", (), {"play": staticmethod(lambda a, r: played.append((len(a), r))), "wait": staticmethod(lambda: None)}))
    assert played and played[0][1] == 24000
    play_reminder_chime(type("Broken", (), {"play": staticmethod(lambda a, r: 1 / 0)}))                      # failure is swallowed


def test_the_app_installs_the_announcer_and_the_chime():
    import inspect

    import ui.app as app

    src = inspect.getsource(app.run)
    assert "reminders.set_announcer(controller.announce)" in src and "controller.chime = play_reminder_chime" in src
