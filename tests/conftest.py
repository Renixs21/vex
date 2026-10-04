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


@pytest.fixture(autouse=True)
def _never_touch_the_real_desktop():
    """Window and keyboard tools run against a fake in tests; the real Win32 backend raises if anything reaches it."""
    from fake_windows import NoWindows
    from safety import context
    from tools import computer

    computer.set_backend(NoWindows())
    context.begin_turn("")
    yield
    computer.set_backend(None)
    context.begin_turn("")
