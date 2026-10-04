import pytest

from memory.viewer_service import ViewerService


@pytest.fixture
def svc(rahul):
    rahul.remember("Lives in Pune", about="me", note_type="profile")
    rahul.remember("Rahul visited [[Goa]] last year", about="Rahul")        # a ghost link: Goa has no note
    return ViewerService(rahul)


def by_title(g, title):
    return next(n for n in g["nodes"] if n["title"] == title)


def test_graph_has_nodes_edges_ghosts_and_link_counts(svc):
    g = svc.graph()
    rahul, site = by_title(g, "Rahul"), by_title(g, "Portfolio Site")
    ids = {n["id"] for n in g["nodes"]}
    assert rahul["links"] >= 2 and site["links"] >= 1 and not rahul["ghost"]
    ghost = by_title(g, "Goa")
    assert ghost["ghost"] and ghost["type"] == "ghost"
    assert all(e["source"] in ids and e["target"] in ids for e in g["edges"])
    assert {rahul["id"], site["id"]} in [{e["source"], e["target"]} for e in g["edges"]]


def test_daily_and_inbox_notes_are_hidden_from_the_graph_unless_asked(svc):
    svc.memory.add_daily_entry("Talked about [[Rahul]]")
    svc.memory.add_to_inbox("Something (proposed for Rahul, person, confidence 0.5; low confidence (0.50))")
    types = {n["type"] for n in svc.graph()["nodes"]}
    assert "daily" not in types and "inbox" not in types
    assert "daily" in {n["type"] for n in svc.graph(show_daily=True)["nodes"]}


def test_note_has_facts_backlinks_tags_and_provenance(svc):
    nid = svc.resolve("Portfolio Site")
    n = svc.note(nid)
    assert n["title"] == "Portfolio Site" and n["type"] == "project" and n["path"].startswith("projects/")
    assert any("Next.js" in f["text"] for f in n["facts"]) and all(f["learned"] for f in n["facts"])
    assert any(b["title"] == "Rahul" and "helping with the portfolio site" in b["text"] for b in n["backlinks"])
    r = svc.note(svc.resolve("my roommate"))                               # aliases resolve
    assert r["title"] == "Rahul" and "my roommate" in r["aliases"]
    assert svc.resolve("Nonexistent") is None and svc.note("nope") is None


def test_sidebar_groups_by_type_lists_recent_and_counts_the_inbox(svc):
    svc.memory.add_to_inbox("Maybe likes tea (proposed for me, preference, confidence 0.6; low confidence (0.60))")
    s = svc.sidebar()
    assert {"Rahul"} <= {n["title"] for n in s["types"]["person"]} and "Profile" in {n["title"] for n in s["types"]["profile"]}
    assert s["inbox"] == 1 and s["recent"] and "inbox" not in s["types"]


def test_search_finds_notes_by_name_alias_and_fact_text(svc):
    assert svc.search("rahul")[0]["title"] == "Rahul"
    assert any(r["title"] == "Rahul" for r in svc.search("roommate"))
    hit = [r for r in svc.search("Next.js") if r["title"] == "Portfolio Site"]
    assert hit and "Next.js" in hit[0]["snippet"]
    assert svc.search("") == []


def test_editing_a_note_saves_atomically_and_updates_the_index(svc):
    nid = svc.resolve("Rahul")
    text = svc.raw(nid)
    assert svc.save(nid, text.replace("Roommate since 2024", "Roommate since 2023"))["ok"]
    assert "Roommate since 2023" in svc.memory.recall("roommate").message


def test_editing_refuses_secrets_and_a_changed_id(svc):
    nid = svc.resolve("Rahul")
    text = svc.raw(nid)
    r = svc.save(nid, text + "\n- My password is hunter22secret\n")
    assert not r["ok"] and "secret" in r["error"]
    r = svc.save(nid, text.replace(nid, "01SOMETHINGELSE00000000000"))
    assert not r["ok"] and "id" in r["error"]
    assert "hunter22" not in svc.raw(nid)
    assert not svc.save("missing-id", text)["ok"]


def test_manual_edit_outside_the_viewer_is_seen_by_version(svc):
    v = svc.version()
    path = svc.index.path_of(svc.resolve("Rahul"))
    path.write_text(path.read_text(encoding="utf-8") + "\n- Plays chess  ^f9\n", encoding="utf-8")
    assert svc.version() != v and any("chess" in f["text"] for f in svc.note(svc.resolve("Rahul"))["facts"])


def test_inbox_lists_proposals_and_approve_moves_them_into_the_right_note(svc):
    svc.memory.add_to_inbox("Rahul plays chess (proposed for Rahul, person, confidence 0.75; personal fact below 0.8 (0.75))")
    svc.memory.add_to_inbox("Odd line the curator never wrote")
    items = svc.inbox()
    chess = next(i for i in items if "chess" in i["fact"])
    assert chess["note"] == "Rahul" and chess["type"] == "person" and chess["confidence"] == 0.75 and not chess["deletion"]
    assert any(i["confidence"] is None for i in items)                                          # free-form lines still show up
    assert svc.approve(chess["path"], chess["block"])["ok"]
    assert "Rahul plays chess" in svc.memory.recall("chess").message
    assert not any("chess" in i["fact"] for i in svc.inbox())


def test_approve_can_correct_the_note_and_discard_removes_without_saving(svc):
    svc.memory.add_to_inbox("Exam is on the 20th (proposed for Exams, topic, confidence 0.6; low confidence (0.60))")
    svc.memory.add_to_inbox("Bad idea (proposed for me, profile, confidence 0.5; low confidence (0.50))")
    a, b = svc.inbox()
    first = next(i for i in (a, b) if "Exam" in i["fact"])
    other = next(i for i in (a, b) if "Bad idea" in i["fact"])
    assert svc.approve(first["path"], first["block"], note="Biology Exam", note_type="topic")["ok"]
    assert svc.resolve("Biology Exam") is not None
    assert svc.discard(other["path"], other["block"])["ok"]
    assert svc.inbox() == [] and "Bad idea" not in "\n".join(p.read_text(encoding="utf-8") for p in svc.vault.iter_paths())


def test_proposed_deletions_can_only_be_discarded(svc):
    svc.memory.add_to_inbox("Old fact (proposed deletion for Rahul, person, confidence 0.9; deletions are proposed, never applied automatically)")
    item = svc.inbox()[0]
    assert item["deletion"]
    r = svc.approve(item["path"], item["block"])
    assert not r["ok"] and svc.inbox()
    assert svc.discard(item["path"], item["block"])["ok"] and svc.inbox() == []
