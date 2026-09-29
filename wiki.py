#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Лаунчер LLM-wiki: python wiki.py [операция] [аргументы]

Кладите этот файл рядом с каталогом llm_wiki/. Всё остальное — внутри пакета.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from llm_wiki.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
