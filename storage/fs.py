"""Права доступа к каталогам с секретами."""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

_DIR_MODE = 0o700
_FILE_MODE = 0o600


def secure_path(path: Path, *, is_dir: bool = True) -> None:
    path.mkdir(parents=True, exist_ok=True) if is_dir else path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, _DIR_MODE if is_dir else _FILE_MODE)
    except OSError as exc:
        logger.debug("chmod skipped for %s: %s", path, exc)


def secure_tree(root: Path) -> None:
    """chmod 700 на каталоги, 600 на файлы внутри data/."""
    if not root.exists():
        secure_path(root, is_dir=True)
        return
    try:
        os.chmod(root, _DIR_MODE)
    except OSError:
        pass
    for item in root.rglob("*"):
        try:
            os.chmod(item, _FILE_MODE if item.is_file() else _DIR_MODE)
        except OSError:
            pass
