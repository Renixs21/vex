"""Keeps Vex alive: runs the app as a child process and restarts it if it dies.

    python main.py --supervise [--startup-delay 15] [--quiet]        (what the Windows autostart entry runs)

  * Clean exit (tray > Quit Vex, exit code 0) -> the supervisor ends too. A deliberate quit is never undone.
  * Another Vex already running (exit code 3) -> the supervisor ends quietly.
  * Anything else (a crash, the process killed) -> restart after a few seconds, at most 3 times in 10 minutes. After that it stops and shows
    a toast with where to look. The child's error output goes to logs/child_stderr.txt, the supervisor's own notes to logs/supervisor.log.
"""
from __future__ import annotations

import logging
import subprocess
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Callable

from ui.recovery import RestartBudget

log = logging.getLogger("vex.supervisor")

EXIT_CLEAN = 0
EXIT_ALREADY_RUNNING = 3
RESTART_DELAY = 3.0


def toast(title: str, message: str) -> None:
    """A Windows toast (patched in tests). Never raises."""
    try:
        from winotify import Notification

        Notification(app_id="Vex", title=title, msg=message, duration="long").show()
    except Exception:  # noqa: BLE001
        log.exception("could not show a toast")


def child_command(root: Path, python: str | None = None, quiet: bool = True, restarted: int = 0) -> list[str]:
    exe = Path(python or sys.executable)
    pyw = exe.with_name("pythonw.exe")
    cmd = [str(pyw if pyw.exists() else exe), str(root / "main.py")]
    if quiet:
        cmd.append("--quiet")
    if restarted:
        cmd += ["--restarted", str(restarted)]
    return cmd


def run(root: Path, logs: Path, startup_delay: float = 0.0, quiet: bool = True, python: str | None = None,
        popen: Callable = subprocess.Popen, sleep: Callable[[float], None] = time.sleep, budget: RestartBudget | None = None,
        notify: Callable[[str, str], None] = toast) -> int:
    """Supervise until a clean quit or until the restart budget is spent. Returns the supervisor's own exit code."""
    budget = budget or RestartBudget()
    logs.mkdir(parents=True, exist_ok=True)
    err_path = logs / "child_stderr.txt"
    try:
        if err_path.exists() and err_path.stat().st_size > 1_000_000:
            err_path.replace(err_path.with_suffix(".old.txt"))
    except OSError:
        pass
    if startup_delay > 0:
        log.info("waiting %.0f s before starting Vex (the desktop is still settling after logon)", startup_delay)
        sleep(startup_delay)
    restarts = 0
    while True:
        cmd = child_command(root, python, quiet, restarts)
        log.info("starting Vex: %s", " ".join(cmd))
        try:
            with open(err_path, "ab") as err:
                proc = popen(cmd, cwd=str(root), stdout=subprocess.DEVNULL, stderr=err,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                code = proc.wait()
        except OSError as e:
            log.error("could not start Vex: %s", e)
            code = -1
        if code == EXIT_CLEAN:
            log.info("Vex quit cleanly; supervisor done")
            return 0
        if code == EXIT_ALREADY_RUNNING:
            log.info("another Vex is already running; supervisor done")
            return 0
        log.warning("Vex ended unexpectedly (exit code %s)", code)
        if not budget.allow():
            vex_log = logs / "vex.log"
            log.error("Vex crashed %d times in %.0f minutes; giving up", budget.maximum + 1, budget.window / 60)
            notify("Vex stopped", f"Vex crashed repeatedly and I've stopped restarting it. Details: {vex_log} and {err_path}. "
                                  "Start it again from the Start menu or: python main.py")
            return 1
        restarts += 1
        sleep(RESTART_DELAY)


def setup_logging(logs: Path) -> None:
    logs.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    if root.handlers:
        return
    root.setLevel(logging.INFO)
    fh = RotatingFileHandler(logs / "supervisor.log", maxBytes=200_000, backupCount=2, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(fh)
