import pytest

import tools
from safety.paths import resolve_path
from tools import registry

tools.load_all()


def test_unknown_tool_returns_error_not_raise():
    r = registry.execute("does_not_exist", {})
    assert not r.ok and "Unknown" in r.message


def test_bad_args_returned_as_message():
    r = registry.execute("create_file", {"nope": 1})
    assert not r.ok and "Bad arguments" in r.message


def test_tool_exception_is_caught():
    @registry.tool("_boom", "x", {})
    def boom():
        raise RuntimeError("kaput")

    r = registry.execute("_boom", {})
    assert not r.ok and "kaput" in r.message


def test_create_file_refuses_overwrite(tmp_path):
    f = tmp_path / "a.txt"
    assert registry.execute("create_file", {"path": str(f), "content": "hi"}).ok
    assert f.read_text() == "hi"
    assert not registry.execute("create_file", {"path": str(f), "content": "again"}).ok
    assert f.read_text() == "hi"


def test_resolve_desktop_prefix():
    assert resolve_path("Desktop/notes.txt").name == "notes.txt"
    assert resolve_path("Desktop/notes.txt").parent.name.lower() == "desktop"


@pytest.mark.parametrize("name", ["create_file", "open_app", "play_youtube", "run_powershell", "forget", "delete_file"])
def test_tools_registered(name):
    assert registry.get(name) is not None


@pytest.mark.parametrize("bad", sorted(registry.RESERVED_ARGS))
def test_tools_cannot_declare_approval_arguments(bad):
    with pytest.raises(ValueError, match="approval"):
        registry.tool("_sneaky", "x", {bad: {"type": "boolean"}})


def test_no_registered_tool_accepts_an_approval_style_argument():
    for t in registry.all_tools():
        assert not (registry.RESERVED_ARGS & set(t.parameters["properties"])), t.name


def test_tiers_of_dangerous_tools():
    assert registry.get("run_powershell").tier == registry.RED
    assert registry.get("delete_file").tier == registry.RED
    assert registry.get("shutdown_pc").tier == registry.RED
    assert registry.get("restart_pc").tier == registry.RED
    assert registry.get("forget").tier == registry.YELLOW
    for n in ("install_app", "uninstall_app", "close_app", "lock_pc"):
        assert registry.get(n).tier == registry.YELLOW
