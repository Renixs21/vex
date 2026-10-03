"""System control: volume, brightness, screenshot, clipboard, system info, lock, shutdown/restart."""
from __future__ import annotations

import subprocess
import time
from datetime import datetime

from safety.paths import known_folder
from tools.registry import RED, YELLOW, ToolResult, tool


SETTLE_SECONDS = 1.5


def _clamp(level: int) -> int:
    return max(0, min(100, int(level)))


def _endpoint():
    """The default speakers' volume interface (patched in tests)."""
    from pycaw.pycaw import AudioUtilities

    return AudioUtilities.GetSpeakers().EndpointVolume


@tool("set_volume", "Set the system volume, 0 to 100.",
      {"level": {"type": "integer", "description": "Volume percent, 0-100"}}, describe=lambda a: f"Set volume to {a['level']}%")
def set_volume(level: int) -> ToolResult:
    level = _clamp(level)
    _endpoint().SetMasterVolumeLevelScalar(level / 100, None)
    return ToolResult(True, f"Volume set to {level}%.")


@tool("mute", "Mute or unmute the system sound.",
      {"muted": {"type": "boolean", "description": "true to mute, false to unmute", "default": True}},
      describe=lambda a: "Mute" if a.get("muted", True) else "Unmute")
def mute(muted: bool = True) -> ToolResult:
    _endpoint().SetMute(1 if muted else 0, None)
    return ToolResult(True, "Muted." if muted else "Unmuted.")


@tool("set_brightness", "Set screen brightness, 0-100.",
      {"level": {"type": "integer", "description": "Brightness percent, 0-100"}}, describe=lambda a: f"Set brightness to {a['level']}%")
def set_brightness(level: int) -> ToolResult:
    import screen_brightness_control as sbc

    level = _clamp(level)
    try:
        sbc.set_brightness(level)
    except Exception as e:  # noqa: BLE001 - many desktop monitors don't support this
        return ToolResult(False, f"This display doesn't allow brightness control: {e}")
    time.sleep(SETTLE_SECONDS)                      # adaptive brightness can undo the change a moment later
    try:
        now = next((b for b in sbc.get_brightness() if b is not None), None)
    except Exception:  # noqa: BLE001
        now = None
    if now is not None and abs(now - level) > 5:
        return ToolResult(False, f"I set the brightness to {level}%, but Windows moved it back to {now}%. Adaptive brightness "
                                 "is probably on (Settings > System > Display > Brightness); turn it off to keep a manual level.")
    return ToolResult(True, f"Brightness set to {level}%.")


@tool("screenshot", "Take a screenshot and save it in Pictures/Vex.", {},
      describe=lambda a: "Take a screenshot (saved in Pictures/Vex)")
def screenshot() -> ToolResult:
    import mss

    folder = known_folder("pictures") / "Vex"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"screenshot-{datetime.now():%Y%m%d-%H%M%S}.png"
    grabber = getattr(mss, "MSS", None) or mss.mss              # mss.mss is deprecated in newer releases
    with grabber() as sct:
        sct.shot(mon=-1, output=str(path))
    return ToolResult(True, f"Saved a screenshot to {path}", {"path": str(path)},
                      undo={"kind": "created", "path": str(path), "label": "screenshot"})


@tool("get_clipboard", "Read the clipboard text (secrets redacted).", {}, audit_args=False,
      describe=lambda a: "Read the clipboard")
def get_clipboard() -> ToolResult:
    import pyperclip

    text = pyperclip.paste() or ""
    return ToolResult(True, text[:2000] if text else "The clipboard is empty.", audit="clipboard read")


@tool("set_clipboard", "Put text on the clipboard.", {"text": {"type": "string", "description": "Text to copy"}},
      audit_args=False, describe=lambda a: f"Copy {len(a['text'])} characters to the clipboard")
def set_clipboard(text: str) -> ToolResult:
    import pyperclip

    pyperclip.copy(text)
    return ToolResult(True, "Copied to the clipboard.", audit="clipboard written")


@tool("system_info", "Battery, CPU, memory and disk usage.", {}, describe=lambda a: "Read system status")
def system_info() -> ToolResult:
    import psutil

    info: dict = {"cpu_percent": psutil.cpu_percent(interval=0.3), "ram_percent": psutil.virtual_memory().percent,
                  "ram_used_gb": round(psutil.virtual_memory().used / 2**30, 1),
                  "ram_total_gb": round(psutil.virtual_memory().total / 2**30, 1)}
    bat = psutil.sensors_battery()
    if bat is not None:
        info["battery_percent"], info["plugged_in"] = round(bat.percent), bat.power_plugged
    disks = []
    for part in psutil.disk_partitions(all=False):
        if "cdrom" in part.opts or not part.fstype:
            continue
        try:
            u = psutil.disk_usage(part.mountpoint)
        except OSError:
            continue
        disks.append({"drive": part.mountpoint, "free_gb": round(u.free / 2**30, 1), "percent_used": u.percent})
    info["disks"] = disks
    msg = f"CPU {info['cpu_percent']:.0f}%, RAM {info['ram_percent']:.0f}% ({info['ram_used_gb']} of {info['ram_total_gb']} GB)."
    if bat is not None:
        msg += f" Battery {info['battery_percent']}%{' (charging)' if info['plugged_in'] else ''}."
    msg += "".join(f" {d['drive']} {d['free_gb']} GB free." for d in disks)
    return ToolResult(True, msg, info)


@tool("lock_pc", "Lock the computer.", {}, tier=YELLOW, describe=lambda a: "Lock the PC")
def lock_pc() -> ToolResult:
    subprocess.run(["rundll32.exe", "user32.dll,LockWorkStation"], check=False)
    return ToolResult(True, "Locked.")


@tool("shutdown_pc", "Shut down in 60 s (cancel_shutdown stops it).", {}, tier=RED,
      describe=lambda a: "Shut down the PC in 60 seconds (cancellable). Command: shutdown /s /t 60")
def shutdown_pc() -> ToolResult:
    r = subprocess.run(["shutdown", "/s", "/t", "60"], capture_output=True, text=True, check=False)
    if r.returncode:
        return ToolResult(False, f"Shutdown failed: {(r.stderr or r.stdout).strip()}")
    return ToolResult(True, "Shutting down in 60 seconds. Say cancel to stop it.")


@tool("restart_pc", "Restart in 60 s (cancel_shutdown stops it).", {}, tier=RED,
      describe=lambda a: "Restart the PC in 60 seconds (cancellable). Command: shutdown /r /t 60")
def restart_pc() -> ToolResult:
    r = subprocess.run(["shutdown", "/r", "/t", "60"], capture_output=True, text=True, check=False)
    if r.returncode:
        return ToolResult(False, f"Restart failed: {(r.stderr or r.stdout).strip()}")
    return ToolResult(True, "Restarting in 60 seconds. Say cancel to stop it.")


@tool("cancel_shutdown", "Cancel a pending shutdown or restart.", {}, describe=lambda a: "Cancel the pending shutdown")
def cancel_shutdown() -> ToolResult:
    r = subprocess.run(["shutdown", "/a"], capture_output=True, text=True, check=False)
    if r.returncode:
        return ToolResult(False, "There's no shutdown pending.")
    return ToolResult(True, "Cancelled the shutdown.")
