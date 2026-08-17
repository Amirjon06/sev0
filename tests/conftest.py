"""Test configuration.

The demo storefront lives outside the installed package, so its source root is
added to the import path here rather than being packaged.
"""

from __future__ import annotations

import sys
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent.parent / "incident_lab" / "app"

if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))
