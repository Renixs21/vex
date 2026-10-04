"""Compile the Control Center's GLSL shaders (vex_app/shaders/*.frag) into Qt's .qsb format with Qt's own qsb tool.

    python tools_dev/build_shaders.py

The .qsb files are committed, so running the app never needs this; run it after changing a .frag.
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SHADERS = ROOT / "vex_app" / "shaders"


def qsb() -> Path:
    import PySide6

    exe = Path(PySide6.__file__).parent / ("qsb.exe" if sys.platform == "win32" else "qsb")
    if not exe.exists():
        raise SystemExit(f"qsb not found next to PySide6 ({exe})")
    return exe


def main() -> int:
    tool = qsb()
    ok = True
    for src in sorted(SHADERS.glob("*.frag")) + sorted(SHADERS.glob("*.vert")):
        out = src.with_name(src.name + ".qsb")
        r = subprocess.run([str(tool), "--glsl", "100es,120,150", "--hlsl", "50", "--msl", "12", "-o", str(out), str(src)],
                           capture_output=True, text=True)
        print(("ok    " if r.returncode == 0 else "FAIL  ") + src.name + (("\n" + r.stdout + r.stderr) if r.returncode else ""))
        ok = ok and r.returncode == 0
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
