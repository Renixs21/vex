"""Vex Control Center: a window onto Vex, through the local API only.

    python -m vex_app                 open it (a second launch brings the open one to the front)
    python -m vex_app --screen tools  open on a screen
    python -m vex_app --screenshot tools_dev/screens/home_1280.png --size 1280x800 --screen home   (dev: save and quit)

Closing the window never stops Vex. If Vex isn't running, the window says so and retries every few seconds.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

log = logging.getLogger("vex_app")

SCREENS = ("home", "conversations", "memory", "tasks", "tools", "voice", "system", "budget")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python -m vex_app", description="Vex Control Center")
    ap.add_argument("--screen", choices=SCREENS, default="home")
    ap.add_argument("--size", default="", help="WxH, e.g. 1280x800")
    ap.add_argument("--screenshot", default="", help="dev: save a PNG of the window after it has loaded, then quit")
    ap.add_argument("--wait", type=float, default=4.0, help="dev: seconds to wait for live data before the screenshot")
    ap.add_argument("--tab", type=int, default=0, help="dev: which tab a tabbed screen opens on (Memory: 0 Focus, 1 Galaxy, 2 Timeline, 3 Notes)")
    ap.add_argument("--port", type=int, default=0, help="dev: a different API port (default: config.toml [api] port)")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    os.environ.setdefault("QT_QUICK_CONTROLS_STYLE", "Basic")       # our own look, not the platform's
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    from PySide6.QtCore import QTimer, QUrl
    from PySide6.QtGui import QFontDatabase, QGuiApplication, QIcon
    from PySide6.QtQml import QQmlApplicationEngine
    from PySide6.QtQuick import QQuickWindow  # noqa: F401 - registers the type, so the root object comes back as a QQuickWindow

    from vex_app import paths
    from vex_app.client import ApiClient
    from vex_app.single import SingleInstance, ask_running_to_focus
    from vex_app.viewmodels import (ConversationsModel, HomeModel, MemoryModel, StatusModel, TasksModel, ToolsModel, VoiceModel,
                                    start_vex_process)

    from PySide6.QtCore import QCoreApplication, Qt
    from PySide6.QtWebEngineQuick import QtWebEngineQuick

    QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts)
    QtWebEngineQuick.initialize()                                    # the Memory graph (sigma.js) runs in a web view
    app = QGuiApplication(sys.argv[:1])
    app.setApplicationName("Vex Control Center")
    app.setOrganizationName("Vex")
    if not args.screenshot and ask_running_to_focus():
        print("The Control Center is already open: brought it to the front.")
        return 0
    for f in paths.FONTS.glob("*.ttf"):
        QFontDatabase.addApplicationFont(str(f))
    app.setWindowIcon(QIcon(str(paths.ICONS / "app.svg")))

    client = ApiClient(args.port or paths.api_port(), paths.read_token)
    status = StatusModel(client, start_vex=lambda: start_vex_process(paths.vex_command()))
    home = HomeModel(client)
    memory = MemoryModel(client)
    memory.toast.connect(status.toast)
    tools = ToolsModel(client)
    tools.toast.connect(status.toast)
    conversations = ConversationsModel(client)
    tasks_model = TasksModel(client)
    tasks_model.toast.connect(status.toast)
    voice_model = VoiceModel(client)
    voice_model.toast.connect(status.toast)
    engine = QQmlApplicationEngine()
    ctx = engine.rootContext()
    ctx.setContextProperty("vex", status)
    ctx.setContextProperty("home", home)
    ctx.setContextProperty("memory", memory)
    ctx.setContextProperty("tools", tools)
    ctx.setContextProperty("conversations", conversations)
    ctx.setContextProperty("tasksModel", tasks_model)
    ctx.setContextProperty("voiceModel", voice_model)
    ctx.setContextProperty("appPaths", {"icons": QUrl.fromLocalFile(str(paths.ICONS) + "/").toString(),
                                        "shaders": QUrl.fromLocalFile(str(paths.SHADERS) + "/").toString(),
                                        "web": QUrl.fromLocalFile(str(paths.WEB) + "/").toString()})
    ctx.setContextProperty("startScreen", args.screen)
    ctx.setContextProperty("startTab", args.tab)
    engine.load(QUrl.fromLocalFile(str(paths.QML / "Main.qml")))
    if not engine.rootObjects():
        log.error("the Control Center's QML failed to load")
        return 1
    window = engine.rootObjects()[0]
    if args.size:
        w, h = (int(v) for v in args.size.lower().split("x"))
        window.setWidth(w)
        window.setHeight(h)

    def focus() -> None:
        window.show()
        window.raise_()
        window.requestActivate()

    single = None if args.screenshot else SingleInstance(focus)
    client.start()

    if args.screenshot:
        out = Path(args.screenshot)
        out.parent.mkdir(parents=True, exist_ok=True)

        def shoot() -> None:
            try:
                img = window.grabWindow()
                img.save(str(out))
                print(f"saved {out} ({img.width()}x{img.height()})")
            finally:
                app.quit()

        QTimer.singleShot(int(args.wait * 1000), shoot)
    code = app.exec()
    client.stop()
    for obj in engine.rootObjects():                         # tear the QML down before the view models it binds to
        obj.deleteLater()
    del engine
    del single
    return code


if __name__ == "__main__":
    raise SystemExit(main())
