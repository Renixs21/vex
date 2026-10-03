from memory import parser


def test_wikilinks_with_alias_and_heading():
    text = "See [[rahul|Rahul]], [[portfolio-site#Plan]] and [[mom]]."
    assert parser.wikilinks(text) == ["rahul", "portfolio-site", "mom"]
    assert parser.strip_links(text) == "See Rahul, portfolio-site and mom."


def test_tags_ignore_headings_links_and_comments():
    body = "# Heading\n- Studies #college and #Food/pizza  ^f1\n- see [[a#section]] <!-- #hidden -->\n"
    assert parser.inline_tags(body) == {"college", "food/pizza"}


def test_blocks_ids_and_provenance():
    body = ("# Rahul\n\n- Roommate since 2024, studies [[mechanical-engineering]] #college  ^f1\n"
            "  <!-- src: daily/2026-10-03 · learned: 2026-10-03 -->\n- Working on [[portfolio-site]]  ^f2\n")
    p = parser.parse_body(body)
    assert [b.block_id for b in p.blocks] == ["f1", "f2"]
    assert p.blocks[0].text.startswith("Roommate since 2024")
    assert (p.blocks[0].source, p.blocks[0].learned) == ("daily/2026-10-03", "2026-10-03")
    assert p.blocks[1].source == ""
    assert p.links == [("mechanical-engineering", "f1"), ("portfolio-site", "f2")]
    assert p.tags == {"college"}


def test_checkboxes_and_code_fences_are_not_facts():
    body = "- [ ] buy milk\n```\n- not a fact  ^f9\n```\n- real fact\n"
    assert parser.parse_body(body).blocks == []
    new, changed = parser.assign_missing_ids(body)
    assert changed and "real fact  ^f1" in new and "buy milk  ^" not in new and "not a fact  ^f9" in new


def test_assign_missing_ids_continues_numbering():
    new, changed = parser.assign_missing_ids("- a  ^f3\n- b\n")
    assert changed and new == "- a  ^f3\n- b  ^f4\n"


def test_rewrite_links_keeps_display_text_and_heading():
    text = "[[Old Name]] and [[old-name|him]] and [[old-name#Plan]] and [[other]]"
    out = parser.rewrite_links(text, {"old name"}, "new-name")
    assert out == "[[new-name]] and [[new-name|him]] and [[new-name#Plan]] and [[other]]"


def test_normalize_and_slugify():
    assert parser.normalize("  My_Room-mate ") == "my room mate"
    assert parser.slugify("Portfolio Site!") == "portfolio-site"
