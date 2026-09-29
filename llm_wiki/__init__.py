# -*- coding: utf-8 -*-
"""LLM-wiki: агент базы знаний поверх GigaChat + конвейер конвертации документов."""

from .config import VERSION as __version__

__all__ = ["__version__", "main"]


def main(argv=None) -> int:
    from .cli import main as _main

    return _main(argv)
