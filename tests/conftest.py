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


@pytest.fixture(autouse=True)
def _no_real_internet_for_web_tools(monkeypatch):
    """The web tools never reach the internet in tests: searches raise unless a test patches them, and HTTP goes to a blocker."""
    import httpx

    from tools import web

    def blocked(request):
        raise AssertionError(f"a test tried to reach the internet: {request.url}")

    def no_search(q, n):
        raise AssertionError("a test tried a real web search")

    monkeypatch.setattr(web, "_transport", httpx.MockTransport(blocked))
    monkeypatch.setattr(web, "_search", no_search)
    monkeypatch.setattr(web, "_search_bing", no_search)
    monkeypatch.setattr(web, "_resolve", lambda host: ["93.184.216.34"])
    web._cache.clear()
    yield
    web._cache.clear()


class FakeRegistry:
    """Stands in for the HKCU Run key: tests never read or write the real registry."""

    def __init__(self):
        self.value = None
        self.log = []

    def get(self):
        return self.value

    def set(self, command):
        self.value = command
        self.log.append(("set", command))

    def delete(self):
        self.value = None
        self.log.append(("delete", None))


@pytest.fixture(autouse=True)
def _autostart_never_touches_the_real_registry(tmp_path_factory):
    from ui import autostart

    reg = FakeRegistry()
    autostart.set_backend(reg)
    autostart.set_state_path(tmp_path_factory.mktemp("autostart") / "autostart.json")
    yield reg
    autostart.set_backend(autostart.RegistryBackend())
    autostart.set_state_path(None)


@pytest.fixture(autouse=True)
def _never_talk_to_the_real_orb(monkeypatch):
    """Tests must never send to the running orb's ports (a stray "quit" closed the real orb once)."""
    from ui import orb_frontend

    real_send = orb_frontend.OrbFrontend._send

    def guarded(self, **msg):
        if self.addr[1] in (47474, 47475):
            return
        return real_send(self, **msg)

    monkeypatch.setattr(orb_frontend.OrbFrontend, "_send", guarded)


@pytest.fixture(autouse=True)
def _switched_off_tools_live_in_a_temp_file(tmp_path_factory):
    """Switching a tool on/off in a test never writes data/tools_disabled.json, and every test starts with all tools on."""
    from tools import registry

    registry.load_disabled(tmp_path_factory.mktemp("tools") / "tools_disabled.json")
    yield
    registry._disabled.clear()


@pytest.fixture(autouse=True)
def _no_real_local_api(monkeypatch):
    """A test that boots the app never opens the real API port or writes data/api_token (API tests start their own server)."""
    import config

    monkeypatch.setitem(config.settings.setdefault("api", {}), "enabled", False)
