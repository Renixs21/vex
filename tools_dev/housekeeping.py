"""Vex housekeeping. See ui/housekeeping.py for exactly what it touches (never your notes).

    python tools_dev/housekeeping.py --dry-run
    python tools_dev/housekeeping.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ui.housekeeping import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
