import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")      # Qt tests run without a display


@pytest.fixture
def mem(tmp_path):
    """A VexMind instance on an empty temp vault."""
    from memory import Memory

    m = Memory(tmp_path / "vault")
    yield m
    m.close()


@pytest.fixture
def rahul(mem):
    """Vault with Rahul (person, alias 'my roommate') linked to the Portfolio Site project."""
    mem.remember("Roommate since 2024", about="Rahul", note_type="person", aliases=["my roommate"])
    mem.remember("Portfolio site is a personal site built with Next.js", about="Portfolio Site", note_type="project")
    mem.remember("Rahul is helping with the portfolio site", about="Rahul")
    mem.remember("Rahul is working on the design", about="Portfolio Site")
    return mem
