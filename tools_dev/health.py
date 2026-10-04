"""Vex health check. See ui/health.py for what it checks.

    python tools_dev/health.py           # free checks only (no API quota)
    python tools_dev/health.py --live    # also tiny real requests (asks first)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ui.health import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
