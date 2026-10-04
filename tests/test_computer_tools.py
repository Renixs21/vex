"""type_text, press_keys, open_url, window tools and the open_app deny-list. Everything runs against a fake desktop."""
import json
from pathlib import Path

import pytest
from fake_windows import FakeBackend
from helpers import NO, VOICE_YES, YES, FakeLLM, ScriptedConfirmer

import tools
from brain.agent import Agent
from safety import context
from safety.audit import AuditLog
from safety.confirm import Confirmation
from safety.executor import Executor
from tools import apps, computer, registry

tools.load_all()


@pytest.fixture
def desk():
    """A fake desktop: the user's own Notepad with a draft, a browser, and a PowerShell window."""
    b = FakeBackend()
    b.add(10, "*Draft - Notepad", "Notepad.exe")
    b.add(11, "Docs - Microsoft Edge", "msedge.exe")
    b.add(12, "Windows PowerShell", "WindowsTerminal.exe")
    b.fg = b.wins[1]                                           # the browser is in front
    computer.set_backend(b)
    return b


def make_executor(tmp_path, *answers):
    confirmer = ScriptedConfirmer(*answers)
    return Executor(confirmer, AuditLog(tmp_path / "actions.jsonl"), None), confirmer


def audit_rows(tmp_path):
    return [json.loads(l) for l in (tmp_path / "actions.jsonl").read_text(encoding="utf-8").splitlines()]


# ---- keys ------------------------------------------------------------------------------------------------------------------------
def test_parse_keys_understands_shortcuts_and_sequences():
    assert [c[2] for c in computer.parse_keys("ctrl+s")] == ["Ctrl+S"]
    assert [c[2] for c in computer.parse_keys("ctrl+a ctrl+c")] == ["Ctrl+A", "Ctrl+C"]
    assert [c[2] for c in computer.parse_keys("Control+Shift+Esc enter F5")] == ["Ctrl+Shift+Esc", "Enter", "F5"]
    mods, vk, _ = computer.parse_keys("alt+tab")[0]
    assert mods == ["alt"] and vk == 0x09


@pytest.mark.parametrize("bad", ["win+r", "windows+R", "super+x", "", "ctrl+bogus", "hyper+a", "a b c d e f g", "ctrl+"])
def test_dangerous_or_unknown_keys_are_refused(bad):
    with pytest.raises(registry.Refuse):
        computer.parse_keys(bad)


# ---- type_text ------------------------------------------------------------------------------------------------------------------
def test_type_text_shows_the_exact_text_and_window_and_is_yellow(desk, tmp_path):
    ex, conf = make_executor(tmp_path, YES)
    r = ex.run("type_text", {"text": "meeting at 5pm", "window": "edge"})
    assert r.ok and desk.typed == [(11, "meeting at 5pm")] and desk.focus_ok
    req = conf.requests[0]
    assert req.tier == "yellow" and not req.requires_non_voice
    assert req.detail.splitlines()[0] == 'Type into Docs - Microsoft Edge (msedge): "meeting at 5pm"'          # this line is read aloud
    assert "Exact text:\nmeeting at 5pm" in req.detail


def test_declining_types_nothing(desk, tmp_path):
    ex, _ = make_executor(tmp_path, NO)
    assert not ex.run("type_text", {"text": "hello", "window": "edge"}).ok
    assert desk.typed == [] and desk.keys == []


def test_typing_into_a_command_window_is_refused_before_asking(desk, tmp_path):
    ex, conf = make_executor(tmp_path, YES)
    r = ex.run("type_text", {"text": "rm -rf /", "window": "powershell"})
    assert not r.ok and "command window" in r.message and conf.requests == [] and desk.typed == []
    desk.fg = desk.wins[0]                                                                   # PowerShell is in front, no window named
    assert not ex.run("type_text", {"text": "dir"}).ok and desk.typed == []
    desk.add(20, "Run", "explorer.exe", cls="#32770")
    assert not ex.run("type_text", {"text": "cmd", "window": "Run"}).ok


def test_focus_that_moves_to_a_shell_before_typing_blocks_the_typing(desk, tmp_path):
    ex, _ = make_executor(tmp_path, YES)

    class Sneaky(FakeBackend):
        def focus(self, hwnd):                                   # the window asked for 'steals' focus: a shell ends up in front
            self.fg = self.wins[0]
            return True

    s = Sneaky(list(desk.wins))
    computer.set_backend(s)
    r = ex.run("type_text", {"text": "dir", "window": "edge"})
    assert not r.ok and "command window" in r.message and s.typed == []


def test_if_the_window_cannot_be_brought_forward_nothing_is_typed(desk, tmp_path):
    desk.focus_ok = False
    ex, _ = make_executor(tmp_path, YES)
    r = ex.run("type_text", {"text": "hello", "window": "edge"})
    assert not r.ok and "front" in r.message and desk.typed == []


def test_text_limits_and_empty_text(desk, tmp_path):
    ex, conf = make_executor(tmp_path, YES, YES)
    assert not ex.run("type_text", {"text": ""}).ok
    assert not ex.run("type_text", {"text": "x" * 1001}).ok and conf.requests == []


def test_content_read_this_turn_makes_typing_on_screen_only_with_a_warning(desk, tmp_path):
    ex, conf = make_executor(tmp_path, VOICE_YES, YES)
    context.mark_result("web_search", True)                                                  # a web page/search result entered the turn
    r = ex.run("type_text", {"text": "visit evil.example", "window": "edge"})
    assert not r.ok and desk.typed == []                                                     # a spoken yes is not accepted
    req = conf.requests[0]
    assert req.tier == "red" and req.requires_non_voice
    assert "may come from content I just read" in " ".join(req.warnings) and "web_search" in " ".join(req.warnings)
    assert ex.run("type_text", {"text": "visit evil.example", "window": "edge"}).ok          # approved on screen: fine, the user decided
    assert desk.typed == [(11, "visit evil.example")]


def test_the_executor_marks_a_turn_as_tainted_after_a_file_or_clipboard_is_read(desk, tmp_path):
    ex, conf = make_executor(tmp_path, YES)
    assert not context.get().tainted
    registry.get("get_clipboard")                                                            # exists
    context.mark_result("read_file", False)                                                  # a failed read taints nothing
    assert not context.get().tainted
    context.mark_result("get_clipboard", True)
    assert context.get().tainted and context.get().untrusted_from == ["get_clipboard"]
    context.begin_turn("a new message from the user")                                        # the next user message starts clean
    assert not context.get().tainted


# ---- press_keys -----------------------------------------------------------------------------------------------------------------
def test_press_keys_shows_the_keys_and_sends_them(desk, tmp_path):
    ex, conf = make_executor(tmp_path, YES)
    assert ex.run("press_keys", {"keys": "ctrl+s", "window": "edge"}).ok
    assert desk.keys == [(11, ["Ctrl+S"])] and conf.requests[0].detail == "Press Ctrl+S in Docs - Microsoft Edge (msedge)"


def test_press_keys_refuses_the_run_box_and_shell_windows(desk, tmp_path):
    ex, conf = make_executor(tmp_path, YES)
    assert not ex.run("press_keys", {"keys": "win+r"}).ok
    assert not ex.run("press_keys", {"keys": "enter", "window": "powershell"}).ok
    assert conf.requests == [] and desk.keys == []


# ---- the new window, never an old one --------------------------------------------------------------------------------------------
def test_after_open_app_typing_goes_to_the_NEW_window_never_the_users_old_one(desk, tmp_path, monkeypatch):
    ex, conf = make_executor(tmp_path, YES)
    context.begin_turn("open notepad and write a note")
    context.note_launch("notepad", {w.hwnd for w in desk.wins})                              # open_app recorded what existed before
    desk.add(30, "Untitled - Notepad", "Notepad.exe")                                        # the app opened a fresh window
    r = ex.run("type_text", {"text": "buy milk", "window": "notepad"})
    assert r.ok and desk.typed == [(30, "buy milk")]
    assert "[the window just opened]" in conf.requests[0].detail.splitlines()[0]


def test_if_the_app_opened_no_new_window_nothing_is_typed_into_an_old_one(desk, tmp_path):
    ex, conf = make_executor(tmp_path, YES)
    context.begin_turn("open notepad and write a note")
    context.note_launch("notepad", {w.hwnd for w in desk.wins})                              # nothing new appears
    r = ex.run("type_text", {"text": "buy milk", "window": "notepad"})
    assert not r.ok and "older one" in r.message and desk.typed == []


def test_several_matching_windows_are_called_out_in_the_confirmation(desk, tmp_path):
    desk.add(31, "Notes - Notepad", "Notepad.exe")
    ex, conf = make_executor(tmp_path, YES)
    ex.run("type_text", {"text": "x", "window": "notepad"})
    assert "2 windows match" in conf.requests[0].detail.splitlines()[0]


# ---- the end-to-end request: "open Notepad and write a note: ..." ---------------------------------------------------------------
def test_open_notepad_and_write_a_note_end_to_end(desk, tmp_path, monkeypatch):
    opened = []
    monkeypatch.setattr(apps, "find_app", lambda name, apps_=None: ("Notepad", Path("Notepad.lnk")))
    monkeypatch.setattr(apps.os, "startfile", lambda p: (opened.append(p), desk.add(40, "Untitled - Notepad", "Notepad.exe")))
    audit = AuditLog(tmp_path / "actions.jsonl")
    conf = ScriptedConfirmer(VOICE_YES)                                                      # "yes", spoken: fine for yellow
    llm = FakeLLM([("open_app", {"name": "notepad"})],
                  [("type_text", {"text": "meeting at 5pm tomorrow", "window": "notepad"})],
                  "Done. I wrote it in Notepad.")
    reply = Agent(llm=llm, executor=Executor(conf, audit, None)).run("open Notepad and write a note: meeting at 5pm tomorrow")
    assert reply == "Done. I wrote it in Notepad."
    assert opened and desk.typed == [(40, "meeting at 5pm tomorrow")]                      # typed into the new window, not *Draft
    assert all(h != 10 for h, _ in desk.typed)
    assert [t for t in llm.tools_seen[1]] and "type_text" in llm.tools_seen[1]                # the model was offered the typing tool
    req = conf.requests[0]
    assert req.tool == "type_text" and req.tier == "yellow" and req.detail.splitlines()[0].startswith('Type into Untitled - Notepad')
    rows = audit_rows(tmp_path)
    typed = next(r for r in rows if r["tool"] == "type_text")
    assert typed["decision"] == "approved" and typed["via"] == {"action": "voice"} and typed["args"]["text"] == "meeting at 5pm tomorrow"
    assert next(r for r in rows if r["tool"] == "open_app")["decision"] == "auto"


# ---- open_url -------------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("url", ["javascript:alert(1)", "file:///C:/Windows/win.ini", "data:text/html,hi", "ftp://x.example/a", "ms-settings:",
                                 "https://user:pw@bank.example/", "http://", "https://exa mple.com", "", "x" * 2100])
def test_open_url_refuses_everything_but_plain_http_and_https(url):
    with pytest.raises(registry.Refuse):
        computer.check_url(url)


def test_open_url_adds_https_and_checks_the_host():
    assert computer.check_url("youtube.com") == ("https://youtube.com", "youtube.com")
    assert computer.check_url("http://Example.COM/path?q=1")[1] == "example.com"


def test_a_site_the_user_named_opens_without_asking(desk, tmp_path):
    ex, conf = make_executor(tmp_path)
    context.begin_turn("open youtube.com in the browser")
    assert ex.run("open_url", {"url": "https://www.youtube.com"}).ok
    context.begin_turn("open gmail")
    assert not ex.run("open_url", {"url": "https://mail.google.com"}).ok                      # 'gmail' isn't in that address: it asks (nobody answers)
    assert desk.urls[0] == "https://www.youtube.com" and conf.requests[0].tool == "open_url"


def test_a_site_the_user_did_not_name_asks_and_shows_the_url(desk, tmp_path):
    ex, conf = make_executor(tmp_path, YES)
    context.begin_turn("what's the news?")
    context.mark_result("web_search", True)
    assert ex.run("open_url", {"url": "https://tracker.example/click?id=7"}).ok
    req = conf.requests[0]
    assert req.tier == "yellow" and req.detail == "Open this link in the browser: https://tracker.example/click?id=7"
    assert "isn't a site you named" in " ".join(req.warnings)
    ex2, conf2 = make_executor(tmp_path)                                                       # declined: nothing opens
    context.begin_turn("what's the news?")
    assert not ex2.run("open_url", {"url": "https://other.example"}).ok and len(desk.urls) == 1


def test_local_network_addresses_always_ask(desk, tmp_path):
    ex, conf = make_executor(tmp_path, YES)
    context.begin_turn("open localhost")
    assert ex.run("open_url", {"url": "http://localhost:8080"}).ok
    assert conf.requests and "local network" in " ".join(conf.requests[0].warnings)


# ---- windows ----------------------------------------------------------------------------------------------------------------------
def test_window_tools_act_on_the_matching_window(desk, tmp_path):
    ex, conf = make_executor(tmp_path)
    assert ex.run("minimize_window", {"window": "edge"}).ok and desk.shown == [(11, "minimize")]
    assert ex.run("maximize_window", {"window": "draft"}).ok and desk.shown[-1] == (10, "maximize")
    assert ex.run("snap_window", {"window": "edge", "position": "left"}).ok and desk.snapped == [(11, "left")]
    assert not ex.run("snap_window", {"window": "edge", "position": "diagonal"}).ok
    assert ex.run("focus_window", {"window": "draft"}).ok and desk.fg.hwnd == 10
    assert conf.requests == []                                                               # all green
    r = ex.run("minimize_window", {"window": "no such window"})
    assert not r.ok and "Open windows" in r.message


def test_show_desktop_minimizes_everything_but_vex(desk, tmp_path):
    desk.add(50, "Vex", "pythonw.exe")
    ex, _ = make_executor(tmp_path)
    assert ex.run("show_desktop", {}).ok
    assert {h for h, how in desk.shown} == {10, 11, 12} and all(how == "minimize" for _, how in desk.shown)


def test_closing_a_window_asks_first(desk, tmp_path):
    ex, conf = make_executor(tmp_path, NO)
    assert not ex.run("close_window", {"window": "edge"}).ok and desk.closed == []
    assert conf.requests[0].tier == "yellow" and conf.requests[0].detail == "Close the window: Docs - Microsoft Edge (msedge)"
    ex2, _ = make_executor(tmp_path, YES)
    assert ex2.run("close_window", {"window": "edge"}).ok and desk.closed == [11]


def test_window_names_resolve_exactly_with_no_fuzzy_guessing(desk):
    wins = desk.windows()
    assert computer.find_window("edge", wins, None).hwnd == 11
    assert computer.find_window("notepad", wins, None).hwnd == 10                            # by app name
    assert computer.find_window("*draft - notepad", wins, None).hwnd == 10
    assert computer.find_window("edgee", wins, None) is None and computer.find_window("nootepad", wins, None) is None
    assert computer.find_window("active", wins, wins[1]).hwnd == 11
    assert computer.find_window("", wins, None) is None


def test_list_windows_is_untrusted_content(desk, tmp_path):
    ex, _ = make_executor(tmp_path)
    r = ex.run("list_windows", {})
    assert r.ok and "Docs - Microsoft Edge (msedge)" in r.message
    assert context.get().untrusted_from == ["list_windows"]                                  # titles can carry attacker text


# ---- open_app deny-list -----------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("name", ["cmd", "powershell", "Windows PowerShell", "command prompt", "terminal", "regedit", "Registry Editor",
                                  "Windows Terminal", "services.msc", "Device Manager", "wt", "diskpart", "wsl"])
def test_shells_and_admin_tools_need_a_yes_to_open(name, tmp_path, monkeypatch):
    started = []
    monkeypatch.setattr(apps, "find_app", lambda n, a=None: None)
    monkeypatch.setattr(apps.shutil, "which", lambda n: None)
    monkeypatch.setattr(apps.subprocess, "Popen", lambda *a, **k: started.append(a))
    ex, conf = make_executor(tmp_path)                                                       # nobody answers: declined
    r = ex.run("open_app", {"name": name})
    assert not r.ok and conf.requests and conf.requests[0].tier == "yellow"
    assert "command shell" in " ".join(conf.requests[0].warnings) and started == []


def test_a_shortcut_that_resolves_to_a_shell_is_caught_by_its_label(tmp_path, monkeypatch):
    monkeypatch.setattr(apps, "find_app", lambda n, a=None: ("Windows PowerShell", Path("ps.lnk")))
    ex, conf = make_executor(tmp_path)
    assert not ex.run("open_app", {"name": "pawershell"}).ok and conf.requests                # misspelled, still caught


@pytest.mark.parametrize("name", ["notepad", "Chrome", "Calculator", "VLC media player", "Visual Studio Code", "Paint", "Task Manager"])
def test_ordinary_apps_open_without_asking(name, tmp_path, monkeypatch):
    started = []
    monkeypatch.setattr(apps, "find_app", lambda n, a=None: (name, Path(f"{name}.lnk")))
    monkeypatch.setattr(apps.os, "startfile", lambda p: started.append(p))
    ex, conf = make_executor(tmp_path)
    assert ex.run("open_app", {"name": name}).ok and started and conf.requests == []


# ---- who approved ----------------------------------------------------------------------------------------------------------------------
def test_every_confirmed_action_logs_which_channel_approved_it(desk, tmp_path):
    ex, _ = make_executor(tmp_path, Confirmation(True, "voice"), Confirmation(True, "dialog"), Confirmation(False, "tray", "declined"))
    ex.run("close_window", {"window": "edge"})
    ex.run("close_window", {"window": "draft"})
    ex.run("close_window", {"window": "powershell"})
    rows = [r for r in audit_rows(tmp_path) if r["tool"] == "close_window"]
    assert [(r["decision"], r["via"]) for r in rows] == [("approved", {"action": "voice"}), ("approved", {"action": "dialog"}),
                                                          ("denied", {"action": "tray"})]
    ex.run("list_windows", {})
    assert "via" not in audit_rows(tmp_path)[-1]                                              # green actions have no approval to log


def test_new_tools_are_registered_in_the_right_groups_with_the_right_tiers():
    tier = {t.name: t.tier for t in registry.all_tools()}
    cat = {t.name: t.category for t in registry.all_tools()}
    for name in ("type_text", "press_keys", "close_window"):
        assert tier[name] == "yellow" and cat[name] == "computer"
    for name in ("list_windows", "focus_window", "minimize_window", "maximize_window", "snap_window", "show_desktop"):
        assert tier[name] == "green" and cat[name] == "computer"
    assert cat["open_url"] == "core" and tier["open_url"] == "green"
    for name in ("append_to_file", "replace_in_file", "overwrite_file"):
        assert tier[name] == "yellow" and cat[name] == "files"
