import os

import pytest

from memory import vault as vault_mod
from memory.secrets import find_secrets
from memory.vault import Vault, dumps, loads


def test_frontmatter_roundtrip_with_awkward_values(tmp_path):
    v = Vault(tmp_path)
    n = v.create("person", "Rahul: K #1", aliases=["my roommate", "R: K"], tags=["friend"])
    back = v.read(n.path)
    assert (back.title, back.aliases, back.tags, back.id) == ("Rahul: K #1", ["my roommate", "R: K"], ["friend"], n.id)
    assert loads(dumps(back)).created == n.created


def test_atomic_write_crash_leaves_original_intact(tmp_path, monkeypatch):
    target = tmp_path / "note.md"
    target.write_text("original", encoding="utf-8")

    def boom(*a, **k):
        raise OSError("disk died")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        vault_mod.atomic_write(target, "new content")
    assert target.read_text(encoding="utf-8") == "original"
    assert [p.name for p in tmp_path.iterdir()] == ["note.md"]          # no temp file left behind


def test_crash_while_writing_temp_file(tmp_path, monkeypatch):
    target = tmp_path / "note.md"
    target.write_text("original", encoding="utf-8")
    monkeypatch.setattr(os, "fsync", lambda fd: (_ for _ in ()).throw(OSError("power cut")))
    with pytest.raises(OSError):
        vault_mod.atomic_write(target, "half written")
    assert target.read_text(encoding="utf-8") == "original"


def test_mutate_keeps_manual_edits(tmp_path):
    v = Vault(tmp_path)
    n = v.create("person", "Rahul")
    v.mutate(n.path, lambda note: Vault.add_fact(note, "likes tea", "test"))
    n.path.write_text(n.path.read_text(encoding="utf-8") + "\nMy own hand-written paragraph.\n", encoding="utf-8")
    v.mutate(n.path, lambda note: Vault.add_fact(note, "likes chess", "test"))
    text = n.path.read_text(encoding="utf-8")
    assert "My own hand-written paragraph." in text and "likes tea" in text and "likes chess" in text
    assert "^f1" in text and "^f2" in text


def test_add_fact_dedupes_and_delete_removes_provenance(tmp_path):
    v = Vault(tmp_path)
    n = v.create("topic", "Food")
    n = v.mutate(n.path, lambda note: (Vault.add_fact(note, "Likes pizza", "t"), Vault.add_fact(note, "likes  pizza", "t")))
    assert len(n.parsed().blocks) == 1
    n = v.mutate(n.path, lambda note: Vault.delete_facts(note, {"f1"}))
    assert "pizza" not in n.body and "src:" not in n.body


@pytest.mark.parametrize("text,kind", [
    ("my key is sk-abcdefghijklmnopqrstuvwx1234", "api key"),
    ("gemini AIzaSyA1234567890abcdefghijklmnopqrstuvw", "api key"),
    ("my card is 4111 1111 1111 1111", "card number"),
    ("password is hunter2", "password"),
    ("the otp is 482913", "pin/otp"),
    ("aadhaar 1234 5678 9012", "aadhaar"),
])
def test_secret_filter_detects(text, kind):
    assert kind in find_secrets(text)


@pytest.mark.parametrize("text", ["Exam is on the 20th", "Phone ends in 4321", "order 1234 5678 9012 3456",
                                  "Rahul studies mechanical engineering", "Born 2004-03-15"])
def test_secret_filter_allows_normal_facts(text):
    assert find_secrets(text) == []


def test_remember_never_writes_secrets(mem):
    for fact in ("My API key is sk-abcdefghijklmnopqrstuvwx1234", "Card number 4111 1111 1111 1111"):
        r = mem.remember(fact, about="Money")
        assert not r.ok and "secret" in r.message
    assert mem.index.resolve("Money") is None
    assert not any("sk-" in p.read_text(encoding="utf-8") for p in mem.vault.iter_paths())
    assert not (mem.vault.root / "topics").exists() or list((mem.vault.root / "topics").iterdir()) == []
