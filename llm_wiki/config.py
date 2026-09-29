# -*- coding: utf-8 -*-
"""Конфигурация LLM-wiki.

Всё, что можно переопределить переменной окружения или флагом CLI, живёт в
единственном объекте :data:`S`. Он мутабельный: меню «SETTINGS» и аргументы
командной строки меняют его поля на месте, поэтому модули импортируют сам
объект (``from .config import S``), а не отдельные значения.
"""

from __future__ import annotations

import os

VERSION = "3.0.0"

# --- неизменяемые константы проекта ---------------------------------------------------

RAW_DIR_NAME = "raw"          # сырые источники, иммутабельны
MD_DIR_NAME = "raw_md"        # markdown-зеркало raw/, генерируется конвертером
WIKI_DIR_NAME = "wiki"        # производная база знаний, единственное место записи

ENCODINGS = ("utf-8-sig", "utf-8", "cp1251", "cp866", "latin-1")
RETRY_STATUSES = {408, 409, 425, 429, 500, 502, 503, 504, 520, 521, 522, 524}
TEXT_EXTENSIONS = {".md", ".txt", ".json", ".jsonl", ".csv", ".tsv", ".yaml", ".yml"}


def _flag(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).lower() not in {"0", "false", "no", ""}


class Settings:
    """Настройки текущего запуска."""

    def __init__(self) -> None:
        # Модель по умолчанию. Замеры на внутреннем шлюзе:
        #   glm-5.2          — потолка генерации нет, но до 300 с на длинную страницу
        #   GigaChat-3-Ultra — стабильна, reasoning-токенов не тратит, ~40 с на страницу
        #   Qwen3.5-397b     — ОБЯЗАТЕЛЕН явный max_tokens, иначе обрыв на 2048 токенах
        self.model = os.getenv("GIGACHAT_MODEL", "glm-5.2")
        self.api_url = os.getenv("GIGACHAT_API_URL", "")
        self.token = os.getenv("JPY_API_TOKEN", "")

        # prompt — JSON-действие текстом ответа; на этом шлюзе стабильнее native
        self.tool_mode = os.getenv("GIGACHAT_TOOL_MODE", "prompt").lower()

        self.max_steps = int(os.getenv("GIGACHAT_MAX_STEPS", "120"))
        self.timeout = int(os.getenv("GIGACHAT_TIMEOUT", "900"))
        self.verify_ssl = os.getenv("GIGACHAT_VERIFY_SSL", "true").lower() not in {"0", "false", "no"}
        self.max_read_chars = int(os.getenv("LLM_WIKI_MAX_READ_CHARS", "100000"))

        # надёжность вызовов API
        self.sleep_between_calls = float(os.getenv("GIGACHAT_SLEEP", "1.0"))
        self.max_attempts = int(os.getenv("GIGACHAT_MAX_ATTEMPTS", "6"))
        self.retry_base_delay = float(os.getenv("GIGACHAT_RETRY_DELAY", "5"))
        self.retry_max_delay = float(os.getenv("GIGACHAT_RETRY_MAX_DELAY", "120"))
        self.empty_retries = int(os.getenv("GIGACHAT_EMPTY_RETRIES", "3"))
        self.max_tokens = int(os.getenv("GIGACHAT_MAX_TOKENS", "8000"))
        self.truncated_retries = int(os.getenv("GIGACHAT_TRUNCATED_RETRIES", "3"))
        self.heartbeat = int(os.getenv("LLM_WIKI_HEARTBEAT", "15"))  # 0 — выключить

        # журнал и бэкапы
        self.state_file = os.getenv("LLM_WIKI_STATE", ".llm_wiki_state.json")
        self.backup_dir = os.getenv("LLM_WIKI_BACKUP_DIR", "backups")
        self.backup_every = int(os.getenv("LLM_WIKI_BACKUP_EVERY", "3"))
        self.backup_keep = int(os.getenv("LLM_WIKI_BACKUP_KEEP", "20"))

        # конвертация
        self.convert_backend = os.getenv("LLM_WIKI_CONVERT_BACKEND", "auto").lower()
        self.image_mode = os.getenv("LLM_WIKI_IMAGE_MODE", "folder").lower()
        self.max_table_rows = int(os.getenv("LLM_WIKI_MAX_TABLE_ROWS", "500"))

        # прочее
        self.verbose = _flag("LLM_WIKI_VERBOSE")
        self.org = os.getenv("LLM_WIKI_ORG", "")


S = Settings()

IGNORED_NAMES = {".git", ".DS_Store", "__pycache__", S.backup_dir, S.state_file, "logs"}
