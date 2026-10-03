import logging
import shutil

import pytest

from memory import forget as forget_mod
from memory import parser


@pytest.fixture
def bin_dir(tmp_path, monkeypatch):
    """Fake Recycle Bin: send2trash moves files here instead of into the real bin."""
    d = tmp_path / "recycle"
    d.mkdir()
    monkeypatch.setattr(forget_mod, "send2trash", lambda p: shutil.move(p, d))
    return d


@pytest.fixture
def team(rahul):
    """Rahul fixture plus a team list and an offhand mention in the project note."""
    rahul.remember("Team: Rahul, Priya and Dev", about="Portfolio Site")
    rahul.remember("Priya works with Rahul on the logo", about="Portfolio Site")
    return rahul


def all_text(mem):
    return "\n".join(p.read_text(encoding="utf-8") for p in mem.vault.iter_paths())


def test_forget_previews_without_changing_anything(team, bin_dir):
    r = team.forget("Rahul")
    assert r.needs_confirmation
    assert 'Recycle Bin: the note "Rahul"' in r.message
    assert "Delete (facts mainly about them):" in r.message and "Rahul is working on the design" in r.message
    assert "Mentions (default: remove just their name" in r.message and "-> Team: Priya and Dev" in r.message
    assert (team.vault.root / "people" / "rahul.md").exists() and not list(bin_dir.iterdir())
    assert "Roommate since 2024" in team.recall("my roommate").message


def test_plan_groups(team):
    plan = forget_mod.build_plan(team.index, "everything about Rahul")
    assert [t for _i, t, _p in plan.notes] == ["Rahul"]
    assert [i.text for i in plan.subject] == ["[[Rahul]] is working on the design"]
    assert sorted(i.new_text for i in plan.mentions) == ["Priya works with someone on the logo", "Team: Priya and Dev"]


def test_forget_person_default_removes_only_the_name(team, bin_dir):
    r = team.forget("everything about Rahul", confirm=True)
    assert r.ok, r.message
    # a) own note is in the (fake) Recycle Bin, gone from the vault and the index
    assert [p.name for p in bin_dir.iterdir()] == ["rahul.md"]
    assert not (team.vault.root / "people" / "rahul.md").exists()
    assert team.index.resolve("Rahul") is None and team.index.resolve("my roommate") is None
    # b) facts about him are deleted
    assert "working on the design" not in all_text(team)
    # c) mentions keep the fact, minus the name
    text = all_text(team)
    assert "Team: Priya and Dev" in text and "Priya works with someone on the logo" in text
    assert "Rahul" not in text and "rahul" not in text.lower().replace("rahul.md", "")
    assert not team.index.search_blocks(["rahul"]) and not team.index.search_blocks(["roommate"])
    assert "personal site" in team.recall("portfolio site").message           # unrelated facts untouched
    assert r.data["notes"] == ["Rahul"] and r.data["deleted"] == 1 and r.data["redacted"] == 2


def test_forget_person_mentions_delete(team, bin_dir):
    r = team.forget("everything about Rahul", confirm=True, mentions="delete")
    assert r.ok and r.data["deleted"] == 3 and r.data["redacted"] == 0
    text = all_text(team)
    assert "Team:" not in text and "logo" not in text and "Priya" not in text


def test_forget_person_choice_per_fact(team, bin_dir):
    plan = forget_mod.build_plan(team.index, "Rahul")
    logo = next(i for i in plan.mentions if "logo" in i.text)
    team.forget("Rahul", confirm=True, choices={logo.key: "delete"})
    text = all_text(team)
    assert "logo" not in text and "Team: Priya and Dev" in text


def test_mention_with_nothing_left_is_deleted(team, bin_dir):
    team.remember("Contact: Rahul", about="Portfolio Site")
    assert forget_mod.redact("Contact: [[Rahul]]", {"rahul"}) is None
    team.forget("Rahul", confirm=True)
    assert "Contact" not in all_text(team)


def test_notes_never_permanently_deleted_if_recycle_bin_fails(team, monkeypatch):
    def broken(_path):
        raise OSError("no recycle bin on this drive")

    monkeypatch.setattr(forget_mod, "send2trash", broken)
    r = team.forget("Rahul", confirm=True)
    assert not r.ok and "Recycle Bin" in r.message
    assert (team.vault.root / "people" / "rahul.md").exists()               # still there, not deleted


def test_forget_logs_only_titles_and_counts(team, bin_dir, caplog):
    with caplog.at_level(logging.DEBUG):
        team.forget("everything about Rahul", confirm=True)
    log_text = caplog.text
    assert "forget: notes=['Rahul'] deleted_facts=1 redacted_facts=2" in log_text
    for secret in ("working on the design", "Priya", "Roommate since 2024", "logo", "Dev"):
        assert secret not in log_text


def test_forget_one_fact_only(rahul, bin_dir):
    r = rahul.forget("Rahul roommate", confirm=True)
    assert r.ok and r.data["deleted"] == 1 and r.data["notes"] == []
    text = (rahul.vault.root / "people" / "rahul.md").read_text(encoding="utf-8")
    assert "Roommate since 2024" not in text and "helping" in text and not list(bin_dir.iterdir())


def test_forget_by_topic_without_a_note_name(mem):
    mem.remember("Exam is on the 20th", about="Exams", note_type="topic")
    mem.remember("Likes jazz", about="Music", note_type="topic")
    assert mem.forget("exam", confirm=True).ok
    assert "20th" not in (mem.vault.root / "topics" / "exams.md").read_text(encoding="utf-8")
    assert "jazz" in mem.recall("music").message


def test_forget_nothing_matching(rahul):
    r = rahul.forget("quantum physics", confirm=True)
    assert r.ok and "anything" in r.message
    assert (rahul.vault.root / "people" / "rahul.md").exists()


@pytest.mark.parametrize("text,subject", [
    ("[[Rahul]] is helping with the site", True),
    ("Rahul's phone is new", True),
    ("Rahul and Priya are on the team", False),
    ("Team: [[Rahul]], Priya", False),
    ("Priya works with Rahul", False),
])
def test_is_subject(text, subject):
    assert forget_mod.is_subject(text, forget_mod.name_regex({"rahul"})) is subject


@pytest.mark.parametrize("text,expected", [
    ("Team: [[rahul|Rahul]], Priya and Dev", "Team: Priya and Dev"),
    ("Priya, Dev and Rahul built it", "Priya, Dev built it"),
    ("Priya works with Rahul", "Priya works with someone"),
    ("Rahul's laptop broke, fix it", "someone's laptop broke, fix it"),
    ("See [[rahul-notes]] about [[Rahul]]", "See [[rahul-notes]] about someone"),
    ("See [[rahul-notes]] and [[Rahul]]", "See [[rahul-notes]]"),
])
def test_redact(text, expected):
    assert forget_mod.redact(text, {"rahul"}) == expected


def test_cli_roundtrip(tmp_path, capsys, monkeypatch, bin_dir):
    from memory.__main__ import main

    v = str(tmp_path / "v")
    assert main(["--vault", v, "remember", "Rahul is my roommate", "--about", "Rahul", "--type", "person",
                 "--alias", "my roommate"]) == 0
    assert main(["--vault", v, "recall", "who is my roommate"]) == 0
    assert "Rahul is my roommate" in capsys.readouterr().out
    monkeypatch.setattr("builtins.input", lambda *_: "n")
    assert main(["--vault", v, "forget", "Rahul"]) == 1
    assert main(["--vault", v, "forget", "Rahul", "--yes"]) == 0
    assert main(["--vault", v, "recall", "who is my roommate"]) == 0
    assert "Nothing relevant" in capsys.readouterr().out.splitlines()[-1]
    assert main(["--vault", v, "reindex"]) == 0


def test_provenance_history_parses():
    body = "- Exam is on the 21st  ^f1\n  <!-- src: user · learned: 2026-10-03 · was: 20th, updated 2026-10-03 -->\n"
    b = parser.parse_body(body).blocks[0]
    assert (b.source, b.learned, b.history) == ("user", "2026-10-03", "was: 20th, updated 2026-10-03")
