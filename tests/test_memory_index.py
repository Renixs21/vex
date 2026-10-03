import time

from memory import Memory
from memory.index import Index
from memory.vault import Vault


def test_rebuild_equals_incremental_sync(rahul):
    rahul.remember("Studies mechanical engineering #college", about="Rahul", tags=["friend"])
    rahul.index.sync()
    incremental = rahul.index.dump()
    rahul.index.rebuild()
    assert rahul.index.dump() == incremental
    fresh = Index(Vault(rahul.vault.root), db_path=rahul.vault.root / ".vexmind" / "other.db")
    fresh.sync()
    assert fresh.dump() == incremental
    fresh.close()


def test_hand_edit_updates_index(rahul):
    path = rahul.vault.root / "people" / "rahul.md"
    path.write_text(path.read_text(encoding="utf-8") + "- Plays the guitar\n", encoding="utf-8")
    rahul.index.sync()
    assert any("guitar" in r["text"] for r in rahul.index.search_blocks(["guitar"]))
    assert path.read_text(encoding="utf-8").rstrip().splitlines()[-1].endswith("^f3")   # bare bullet got a stable id


def test_watcher_picks_up_hand_edits(rahul):
    rahul.index.start_watching(debounce=0.2)
    try:
        path = rahul.vault.root / "people" / "rahul.md"
        path.write_text(path.read_text(encoding="utf-8") + "- Collects vintage telescopes  ^f99\n", encoding="utf-8")
        deadline = time.time() + 5
        while time.time() < deadline and not rahul.index.search_blocks(["telescopes"]):
            time.sleep(0.1)
        assert rahul.index.search_blocks(["telescopes"])
    finally:
        rahul.index.stop_watching()


def test_handmade_note_without_frontmatter_is_adopted(mem):
    (mem.vault.root / "topics").mkdir()
    p = mem.vault.root / "topics" / "fitness.md"
    p.write_text("# Fitness\n\n- Goes to the gym on Mondays #health\n", encoding="utf-8")
    mem.index.sync()
    row = mem.index.resolve("Fitness")
    assert row and row["type"] == "topic" and row["id"]
    assert mem.vault.read(p).id == row["id"]                       # identity was written back to the file
    assert [r["title"] for r in mem.index.list_notes(tag="health")] == ["Fitness"]


def test_deleted_file_leaves_index(rahul):
    (rahul.vault.root / "people" / "rahul.md").unlink()
    rahul.index.sync()
    assert rahul.index.resolve("Rahul") is None
    assert rahul.index.resolve("my roommate") is None
    assert not rahul.index.search_blocks(["roommate"])


def test_rename_updates_links_everywhere(rahul):
    before = rahul.index.stats()["ghost_links"]
    assert rahul.rename_note("Portfolio Site", "Personal Website").ok
    assert not (rahul.vault.root / "projects" / "portfolio-site.md").exists()
    assert (rahul.vault.root / "projects" / "personal-website.md").exists()
    assert rahul.index.resolve("Personal Website")
    assert "[[portfolio" not in "".join(p.read_text(encoding="utf-8") for p in rahul.vault.iter_paths()).lower()
    assert rahul.index.stats()["ghost_links"] == before            # no link was orphaned by the rename
    out, back = rahul.index.neighbors(rahul.index.resolve("Rahul")["id"])
    assert rahul.index.resolve("Personal Website")["id"] in out + back


def test_copied_note_gets_its_own_identity(rahul):
    src = rahul.vault.root / "people" / "rahul.md"
    (rahul.vault.root / "people" / "rahul-copy.md").write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    rahul.index.sync()
    ids = [r["id"] for r in rahul.index.list_notes()]
    assert len(ids) == len(set(ids)) == 3


def test_reopening_keeps_index(tmp_path):
    m = Memory(tmp_path / "v")
    m.remember("Likes jazz", about="Music", note_type="topic")
    m.close()
    m2 = Memory(tmp_path / "v")
    assert m2.index.sync() == {"changed": 0, "removed": 0}
    assert "jazz" in m2.recall("what music do I like").message
    m2.close()
