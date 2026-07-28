"""Small runtime helpers shared by analysis entry points."""

from __future__ import annotations

import os
from pathlib import Path

from .ramulator import repo_root


def configure_matplotlib_cache(root: Path | None = None) -> None:
    cache_dir = (root or repo_root()) / "output" / "matplotlib-cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("MPLCONFIGDIR", str(cache_dir))
