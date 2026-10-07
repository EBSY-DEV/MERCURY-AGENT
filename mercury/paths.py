"""Canonical filesystem locations for the Mercury checkout.

Mercury is repo-centric: config (mercury.yaml, .env), knowledge (prompts/,
skills/), and state (data/) all live next to the package. ``resolve()``
matters — when the package is reached through a symlink (some editable
installs), a bare ``Path(__file__).parent`` would point into
site-packages and Mercury would silently read/write the wrong files.
"""

import logging
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Mercury was previously called Harvey. Checkouts from before the rename
# carry harvey.* files; move them over once so nobody loses their pipeline.
_LEGACY_RENAMES = [
    ("harvey.local.yaml", "mercury.local.yaml"),
    ("data/harvey.db", "data/mercury.db"),
    ("data/harvey.db-wal", "data/mercury.db-wal"),
    ("data/harvey.db-shm", "data/mercury.db-shm"),
    ("data/harvey.log", "data/mercury.log"),
]


def migrate_legacy_files(root: Path = PROJECT_ROOT) -> list[str]:
    """Rename pre-rebrand files in place. Idempotent; never overwrites."""
    moved = []
    for old, new in _LEGACY_RENAMES:
        src, dst = root / old, root / new
        if src.exists() and not dst.exists():
            try:
                src.rename(dst)
                moved.append(f"{old} -> {new}")
            except OSError:
                pass
    if moved:
        logging.getLogger("mercury.paths").info("Migrated legacy files: %s", ", ".join(moved))
    return moved


migrate_legacy_files()
