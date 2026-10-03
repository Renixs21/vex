from memory.retrieve import terms


def test_terms_drop_stopwords():
    assert terms("What's my roommate working on?") == ["roommate", "working"]


def test_roommate_question_returns_fact_and_linked_project(rahul):
    out = rahul.recall("what's my roommate working on?").message
    assert "Roommate since 2024" in out
    assert "[Portfolio Site]" in out                               # reached through the link, not the words


def test_who_is_helping_with_project(rahul):
    out = rahul.recall("who's helping me with my portfolio site?").message
    assert "Rahul is helping with the portfolio site" in out
    assert out.index("[Portfolio Site]") >= 0


def test_alias_resolution_finds_note(rahul):
    assert "Roommate since 2024" in rahul.recall("tell me about my roommate").message


def test_fuzzy_name_match(rahul):
    assert "Roommate since 2024" in rahul.recall("is Rahull coming over?").message


def test_always_on_profile_and_preferences(mem):
    mem.remember("Name is Sanjay", about="me", note_type="profile")
    mem.remember("Keep answers short", note_type="preference")
    mem.remember("Exam is on the 20th", about="Exams", note_type="topic")
    pack = mem.retrieve("when is my exam")
    assert "[Profile] Name is Sanjay" in pack.text and "[Preferences] Keep answers short" in pack.text
    assert any("Exam is on the 20th" in i.text for i in pack.items)


def test_unrelated_query_returns_nothing_extra(rahul):
    assert rahul.recall("quantum chromodynamics").message == "Nothing relevant in memory."


def test_budget_is_respected(mem):
    for i in range(60):
        mem.remember(f"Fact number {i} about gardening and plants and soil " * 3, about=f"Garden {i}")
    pack = mem.retrieve("gardening plants soil", budget_tokens=200)
    assert pack.tokens <= 200 and pack.items


def test_updating_a_reworded_fact_does_not_duplicate(mem):
    mem.remember("Exam is on the 20th", about="Exams", note_type="topic")
    r = mem.remember("Exam is on the 21st", about="Exams")
    assert r.data["action"] == "updated"
    assert mem.remember("Exam is on the 21st", about="Exams").data["action"] == "duplicate"
    assert mem.memory_stats().data["facts"] == 1
    assert "21st" in mem.recall("when is the exam").message and "20th" not in mem.recall("when is the exam").message


def test_remember_autolinks_known_notes(rahul):
    rahul.remember("Went hiking with Rahul last weekend", about="Hiking", note_type="topic")
    text = (rahul.vault.root / "topics" / "hiking.md").read_text(encoding="utf-8")
    assert "[[Rahul]]" in text
    assert rahul.index.neighbors(rahul.index.resolve("Hiking")["id"])[0] == [rahul.index.resolve("Rahul")["id"]]


def test_link_notes_and_list_and_read(rahul):
    assert rahul.link_notes("Rahul", "Portfolio Site", "co-owner").ok
    assert not rahul.link_notes("Rahul", "Nobody").ok
    assert "co-owner" in rahul.read_note("rahul").message
    assert "person: Rahul" in rahul.list_notes("person").message
    assert not rahul.read_note("nope").ok


def test_changed_value_is_traceable_in_provenance(mem):
    from memory.vault import today

    mem.remember("Exam is on the 20th", about="Exams", note_type="topic")
    mem.remember("Exam is on the 21st", about="Exams")
    mem.remember("Exam is on the 22nd", about="Exams")
    text = (mem.vault.root / "topics" / "exams.md").read_text(encoding="utf-8")
    assert f"was: 21st, updated {today()}; was: 20th, updated {today()}" in text
    assert text.count("^f") == 1 and "22nd" in text
    mem.index.rebuild()
    assert "22nd" in mem.recall("when is the exam").message             # still indexes and retrieves normally
    row = mem.index.db.execute("SELECT learned FROM blocks").fetchone()
    assert row["learned"] == today()


def test_reworded_update_records_old_text(mem):
    mem.remember("Rahul studies mechanical engineering at the university", about="Rahul", note_type="person")
    mem.remember("Rahul studies mechanical engineering at the university.", about="Rahul")
    assert "was: Rahul studies mechanical engineering at the university" in (
        mem.vault.root / "people" / "rahul.md").read_text(encoding="utf-8")
