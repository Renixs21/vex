"""clean_reply against replies the Groq fallback model actually produced during the Phase 1/2 smoke runs."""
import pytest

from brain.sanitize import clean_reply

ZW = "​"

# (what the model returned, what the user should see/hear)
REAL_LEAKS = [
    # reasoning notes leaked into the reply, after filler made of zero-width characters and ellipses
    ("Brightness … " + ZW * 2 + "  " + ZW + " " + ZW + " \n\n\n\nIt looks like the answer **…**…\n\n"
     "The response got garbled. Need to give concise reply confirming brightness set.Brightness set to 92 percent.",
     "Brightness set to 92 percent."),
    ("Shutting " + ZW * 5 + "  " + ZW + " " + ZW + "  " + ZW + "  " + ZW + "  …  …  …  ……  " + ZW + "  \n\n\n\n"
     "The output should be clearly  …  ...... \n\n\n\nThe  ...\n\n\n\nNeed to give clear concise confirmation."
     "Shutting down in 60 seconds.",
     "Shutting down in 60 seconds."),
    # a draft followed by the final answer
    ("Brightness  —  40 %  done.Brightness set to 40 percent.", "Brightness set to 40 percent."),
    ("File created on Desktop as bench_note.txt with “hi”.File created on your desktop.", "File created on your desktop."),
    ("The date is 2026-10-03.Date: 2026-10-03.", "Date: 2026-10-03."),
    # sentences glued together without a space
    ("Muted.Done.", "Muted. Done."),
    ("Unmuted.All done.", "Unmuted. All done."),
    ("Brightness set to 40 percent.Done.", "Brightness set to 40 percent. Done."),
    ("The file has been restored to your desktop.Anything else I can help with?",
     "The file has been restored to your desktop. Anything else I can help with?"),
    ("Today's date is 2026‑10‑03.You're all set. Let me know if you need anything else.",
     "Today's date is 2026-10-03. You're all set. Let me know if you need anything else."),
    ("The command was not approved; it was not executed.Do you want to approve running `Get-Service wuauserv` as administrator?",
     "The command was not approved; it was not executed. Do you want to approve running Get-Service wuauserv as administrator?"),
    # markdown in a spoken reply
    ("Do you approve running this PowerShell command?\n\n**Command:** `Get-Process | Select-Object -First 3 Name`",
     "Do you approve running this PowerShell command? Command: Get-Process | Select-Object -First 3 Name"),
    # narrow no-break spaces and dashes the speech engine would stumble on
    ("Remembered – Roommate – Rahul.Your roommate Rahul has been saved in memory.",
     "Remembered – Roommate – Rahul. Your roommate Rahul has been saved in memory."),
]


@pytest.mark.parametrize("raw,expected", REAL_LEAKS)
def test_real_leaked_replies_are_cleaned(raw, expected):
    assert clean_reply(raw) == expected


@pytest.mark.parametrize("reply", [
    "Two plus two equals four.",
    "I need to know which file you mean.",
    "I need to confirm which package you mean.",
    "The answer is four.",
    "The user folder is empty.",
    "Let me know if you need anything else.",
    "Let me check that for you.",
    "Okay, I opened Notepad.",
    "The output file is on your desktop.",
    "The response was 200 OK from the server.",
    "Need more details? Tell me the file name.",
    "Opened Notepad. Opened it for you.",
    "You have 3 tasks: buy milk, call the dentist, and finish the report.",
    "Version 3.1 is installed.",
    "Done.",
    "It's 4:15 p.m. on Friday, October 3rd.",
    "The U.S.A. team won.",
    "Rahul is your roommate. Rahul is also helping with the portfolio site.",
])
def test_ordinary_replies_are_left_alone(reply):
    assert clean_reply(reply) == reply


def test_notes_after_the_answer_are_dropped_but_the_answer_is_kept():
    assert clean_reply("Volume set to 20 percent. Need to give concise confirmation.") == "Volume set to 20 percent."


def test_internal_notes_before_the_answer_are_dropped():
    raw = "The user wants the volume changed. We need to respond briefly. Volume is now 20 percent."
    assert clean_reply(raw) == "Volume is now 20 percent."


def test_a_reply_that_is_only_notes_becomes_empty():
    assert clean_reply("The user wants a file. Need to give a short answer.") == ""
    assert clean_reply("… ​ ​ ...") == ""
    assert clean_reply("") == ""


def test_output_never_contains_invisible_or_odd_spacing_characters():
    for raw, _ in REAL_LEAKS:
        out = clean_reply(raw)
        assert not any(ch in out for ch in "​‌‍⁠﻿  ‑")
        assert "  " not in out and "**" not in out and "`" not in out and "…" not in out and "..." not in out


def test_the_agent_returns_the_cleaned_reply_and_stores_it(tmp_path, monkeypatch):
    import tools
    from brain.agent import Agent
    from helpers import FakeLLM, ScriptedConfirmer
    from memory import Memory
    from safety.audit import AuditLog
    from safety.executor import Executor

    tools.load_all()
    mem = Memory(tmp_path / "v")
    ex = Executor(ScriptedConfirmer(), AuditLog(tmp_path / "a.jsonl"), None)
    raw = "Brightness … " + ZW + "\n\nNeed to give concise reply.Brightness set to 40 percent."
    agent = Agent(llm=FakeLLM(raw, "Muted.Done."), executor=ex, memory=mem)
    assert agent.run("set brightness") == "Brightness set to 40 percent."
    assert agent.history[-1]["text"] == "Brightness set to 40 percent."            # the leak isn't fed back to the model
    assert agent.run("mute") == "Muted. Done."
    mem.close()


def test_the_agent_says_done_if_the_reply_was_only_notes(tmp_path):
    import tools
    from brain.agent import Agent
    from helpers import FakeLLM, ScriptedConfirmer
    from memory import Memory
    from safety.audit import AuditLog
    from safety.executor import Executor

    tools.load_all()
    mem = Memory(tmp_path / "v")
    agent = Agent(llm=FakeLLM("The user wants it. Need to give a short answer."),
                  executor=Executor(ScriptedConfirmer(), AuditLog(tmp_path / "a.jsonl"), None), memory=mem)
    assert agent.run("hi") == "Done."
    mem.close()
