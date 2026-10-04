"""The real viewer window (QtWebEngine + QWebChannel + ui/memory.html), driven headlessly in a child process.

A child process keeps a browser-engine crash from taking pytest down; the page's own JavaScript is asked what it rendered.
"""
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

SCRIPT = textwrap.dedent("""
    import json, sys, tempfile
    from pathlib import Path
    sys.path.insert(0, %r)
    from PySide6.QtCore import QCoreApplication, Qt
    QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts)
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
    from memory import Memory
    from memory.viewer_service import ViewerService
    from ui.memory_viewer import MemoryViewerWindow

    mem = Memory(Path(tempfile.mkdtemp()) / "vault")
    mem.remember("Roommate since 2024", about="Rahul", note_type="person", aliases=["my roommate"])
    mem.remember("Portfolio site is built with Next.js", about="Portfolio Site", note_type="project")
    mem.remember("Rahul is helping with the portfolio site", about="Rahul")
    mem.add_to_inbox("Rahul plays chess (proposed for Rahul, person, confidence 0.75; personal fact below 0.8 (0.75))")
    app = QApplication([])
    w = MemoryViewerWindow(ViewerService(mem))
    w.show()
    out = {}
    def js(code):
        box = []
        w.view.page().runJavaScript(code, lambda r: box.append(r))
        for _ in range(100):
            if box: break
            QTest.qWait(100)
        return box[0] if box else None
    for _ in range(200):                      # up to 20 s for the page to load and the bridge to connect
        if w.bridge.ready_calls: break
        QTest.qWait(100)
    out["ready"] = w.bridge.ready_calls
    for _ in range(100):
        if js("data.nodes.length") : break
        QTest.qWait(100)
    out["nodes"] = js("data.nodes.length")
    out["edges"] = js("data.edges.length")
    out["badge"] = js("document.querySelector('#badge').textContent")
    out["side"] = js("document.querySelectorAll('#list .item').length")
    js("openNote(data.nodes.find(n => n.title === 'Rahul').id)")
    QTest.qWait(1200)
    out["note_title"] = js("document.querySelector('#note h1') && document.querySelector('#note h1').textContent")
    out["backlinks"] = js("document.querySelectorAll('#note .back').length")
    out["facts"] = js("document.querySelectorAll('#note .fact-li').length")
    out["unknown"] = w.bridge.call("os_system", "[]")
    print("RESULT " + json.dumps(out))
    sys.stdout.flush()
    import os; os._exit(0)
""" % str(ROOT))


@pytest.mark.skipif(sys.platform != "win32", reason="the viewer is only used on Windows")
def test_the_viewer_page_loads_connects_to_python_and_renders_notes():
    env = {**os.environ, "QT_QPA_PLATFORM": "offscreen", "QTWEBENGINE_CHROMIUM_FLAGS": "--disable-gpu --no-sandbox"}
    p = subprocess.run([sys.executable, "-c", SCRIPT], capture_output=True, text=True, timeout=120, env=env, cwd=str(ROOT))
    line = next((l for l in p.stdout.splitlines() if l.startswith("RESULT ")), None)
    assert line, f"no result\nSTDOUT {p.stdout[-800:]}\nSTDERR {p.stderr[-1500:]}"
    out = json.loads(line[7:])
    assert out["ready"] == 1                                    # the page connected to the bridge
    assert out["nodes"] == 2 and out["edges"] == 1              # Rahul, Portfolio Site, linked
    assert out["badge"] == "1"                                  # one inbox item
    assert out["side"] >= 2                                     # sidebar lists the notes
    assert out["note_title"] == "Rahul" and out["facts"] == 2   # note panel rendered
    assert json.loads(out["unknown"])["ok"] is False            # the bridge only answers whitelisted calls
