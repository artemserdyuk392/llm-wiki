# -*- coding: utf-8 -*-
"""Журнал обработанных источников и бэкапы каталога wiki/."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import IGNORED_NAMES, RAW_DIR_NAME, S, WIKI_DIR_NAME
from .ui import log


def state_path(root: Path) -> Path:
    return root / S.state_file


def load_state(root: Path) -> Dict[str, Any]:
    path = state_path(root)
    empty: Dict[str, Any] = {"version": 2, "ingested": {}, "converted": {}}
    if not path.is_file():
        return empty
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        log(f"ВНИМАНИЕ: журнал {path.name} повреждён ({exc}), начинаю с пустого")
        return empty
    if not isinstance(data, dict):
        return empty
    data.setdefault("ingested", {})
    data.setdefault("converted", {})
    return data


def save_state(root: Path, state: Dict[str, Any]) -> None:
    path = state_path(root)
    fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", dir=str(root))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(state, file, ensure_ascii=False, indent=2)
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def file_fingerprint(path: Path) -> Dict[str, Any]:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(65536), b""):
            digest.update(chunk)
    return {"sha256": digest.hexdigest(), "size": path.stat().st_size}


def is_ingested(state: Dict[str, Any], rel_path: str, fingerprint: Dict[str, Any]) -> bool:
    entry = state["ingested"].get(rel_path)
    if not isinstance(entry, dict):
        return False
    return entry.get("sha256") == fingerprint["sha256"]  # изменённый источник обрабатывается заново


def mark_ingested(state: Dict[str, Any], rel_path: str, fingerprint: Dict[str, Any]) -> None:
    state["ingested"][rel_path] = {
        "sha256": fingerprint["sha256"],
        "size": fingerprint["size"],
        "ingested_at": datetime.now().isoformat(timespec="seconds"),
    }


def collect_raw_files(root: Path) -> List[str]:
    """Все файлы из raw/ в виде путей относительно корня проекта."""
    raw_root = (root / RAW_DIR_NAME).resolve()
    files: List[str] = []
    if not raw_root.is_dir():
        return files
    for item in sorted(raw_root.rglob("*")):
        if not item.is_file() or item.name.startswith("."):
            continue
        if any(part in IGNORED_NAMES for part in item.relative_to(root).parts):
            continue
        files.append(item.relative_to(root).as_posix())
    return files


def backup_wiki(root: Path, tag: str = "") -> Optional[Path]:
    wiki = root / WIKI_DIR_NAME
    if not wiki.is_dir():
        return None

    backups = root / S.backup_dir
    backups.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    suffix = f"-{re.sub(r'[^0-9A-Za-zА-Яа-я_.-]+', '_', tag)}" if tag else ""
    dest = backups / f"wiki-{stamp}{suffix}"
    counter = 1
    while dest.exists():
        dest = backups / f"wiki-{stamp}{suffix}-{counter}"
        counter += 1

    shutil.copytree(wiki, dest)
    prune_backups(backups)
    log(f"  [backup] {dest.relative_to(root).as_posix()}")
    return dest


def prune_backups(backups: Path) -> None:
    if S.backup_keep <= 0:
        return
    items = sorted(p for p in backups.iterdir() if p.is_dir() and p.name.startswith("wiki-"))
    for old in items[: -S.backup_keep]:
        shutil.rmtree(old, ignore_errors=True)
