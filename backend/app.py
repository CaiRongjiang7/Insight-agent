from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from repo_maintainer.api import create_app
from repo_maintainer.config import PlatformConfig


app = create_app(PlatformConfig.default(ROOT))
