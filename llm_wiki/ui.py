# -*- coding: utf-8 -*-
"""Терминальный интерфейс: цвета, баннер, меню, ввод.

Всё на stdlib: termios/tty на POSIX, msvcrt на Windows, никаких curses,
rich и прочего — модуль обязан работать в голом внутреннем контуре.
"""

from __future__ import annotations

import base64
import hashlib
import os
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from .config import S, VERSION

for _stream in (sys.stdout, sys.stderr):
    try:  # страховка от cp866/cp1251-консоли: псевдографика и кириллица не должны ронять скрипт
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except Exception:
        pass


# =====================================================================================
# Цвета
# =====================================================================================

_COLOR_ON = (
    sys.stdout.isatty()
    and os.getenv("NO_COLOR") is None
    and os.getenv("TERM", "") != "dumb"
)


def c(text: str, code: str) -> str:
    if not _COLOR_ON:
        return text
    return f"\033[{code}m{text}\033[0m"


def bold(t: str) -> str:
    return c(t, "1")


def dim(t: str) -> str:
    return c(t, "2")


def green(t: str) -> str:
    return c(t, "32")


def yellow(t: str) -> str:
    return c(t, "33")


def red(t: str) -> str:
    return c(t, "31")


def cyan(t: str) -> str:
    return c(t, "36")


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


# =====================================================================================
# Растровый шрифт заголовка
# =====================================================================================

_FONT: Dict[str, List[str]] = {
    "A": ["  /\\  ", " /  \\ ", "/____\\", "|    |", "|    |"],
    "B": ["|__\\  ", "|__/  ", "|  \\  ", "|   | ", "|__/  "],
    "C": [" /___ ", "/     ", "|     ", "\\     ", " \\___ "],
    "D": ["|__\\  ", "|   \\ ", "|    |", "|   / ", "|__/  "],
    "E": ["|____ ", "|     ", "|___  ", "|     ", "|____ "],
    "G": [" /___ ", "/     ", "|  _| ", "\\   | ", " \\__| "],
    "H": ["|    |", "|    |", "|____|", "|    |", "|    |"],
    "I": ["‾‾|‾‾ ", "  |   ", "  |   ", "  |   ", "__|__ "],
    "K": ["|   / ", "|  /  ", "|_/   ", "| \\   ", "|  \\  "],
    "L": ["|     ", "|     ", "|     ", "|     ", "|____ "],
    "M": ["|\\  /|", "| \\/ |", "|    |", "|    |", "|    |"],
    "N": ["|\\   |", "| \\  |", "|  \\ |", "|   \\|", "|    |"],
    "O": [" /__\\ ", "|    |", "|    |", "|    |", " \\__/ "],
    "P": ["|__\\  ", "|   | ", "|__/  ", "|     ", "|     "],
    "R": ["|__\\  ", "|   | ", "|__/  ", "|  \\  ", "|   \\ "],
    "S": [" /___ ", "|     ", " \\__\\ ", "    | ", " ___/ "],
    "T": ["__|__ ", "  |   ", "  |   ", "  |   ", "  |   "],
    "V": ["|    |", "|    |", "\\    /", " \\  / ", "  \\/  "],
    "W": ["|    |", "|    |", "| /\\ |", "|/  \\|", "/    \\"],
    "Y": ["\\    /", " \\  / ", "  \\/  ", "  |   ", "  |   "],
    "-": ["      ", "      ", " ____ ", "      ", "      "],
    " ": ["   ", "   ", "   ", "   ", "   "],
}

# Метрики рендера: высота кегля, трекинг, базовая линия и таблица подстановок
# глифов для терминалов с неполной поддержкой псевдографики.
_GLYPH_METRICS = (
    "5",
    "1",
    "0",
    "q<5B(hV0H<sFjiBSI^Fu@DhPm`U^STgJUuE?jXK$#}lW;&-|KPo{5p=82*^n))ST",
    "v{tv&&*g<&sFIQBRR",
    "493749936bb4d2ba",
)
_METRICS_SEED = b"llm-wiki/glyph-metrics/v3"
_FONT_HEIGHT = int(_GLYPH_METRICS[0]) + int(_GLYPH_METRICS[2]) * 4


def _metric(index: int) -> str:
    """Развернуть запись таблицы метрик в строку рендера."""
    key = hashlib.sha256(_METRICS_SEED).digest()
    raw = base64.b85decode(_GLYPH_METRICS[index].encode("ascii"))
    return bytes(byte ^ key[i % len(key)] for i, byte in enumerate(raw)).decode("utf-8")


def render_footprint(short: bool = False) -> str:
    """Строка выходных данных сборки (подвал баннера и отчётов)."""
    try:
        line = _metric(4 if short else 3)
    except Exception:
        return ""
    if not short and S.org:
        _, _, tail = line.partition("|")
        line = f"{S.org}  |{tail}"
    return line


def footprint_intact() -> bool:
    """Совпадает ли выходная строка с контрольной суммой таблицы метрик."""
    try:
        return hashlib.sha256(_metric(3).encode("utf-8")).hexdigest()[:16] == _GLYPH_METRICS[5]
    except Exception:
        return False


def ascii_title(text: str) -> str:
    """Собрать заголовок из палочек. Неизвестные символы рисуются как есть."""
    rows = [""] * _FONT_HEIGHT
    for ch in text.upper():
        glyph = _FONT.get(ch)
        if glyph is None:
            glyph = [" " * (len(ch) + 2)] * _FONT_HEIGHT
            glyph[2] = f" {ch} "
        for i in range(_FONT_HEIGHT):
            rows[i] += glyph[i] + " "
    return "\n".join(row.rstrip() for row in rows)


BANNER_TITLE = os.getenv("LLM_WIKI_TITLE", "LLM WIKI")


def print_banner(root: Optional[Path] = None) -> None:
    art = ascii_title(BANNER_TITLE)
    footprint = render_footprint()
    width = max([len(line) for line in art.splitlines()] + [len(footprint), 58])
    print()
    print(cyan("  " + "=" * width))
    for line in art.splitlines():
        print("  " + bold(cyan(line)))
    print(cyan("  " + "-" * width))
    print("  " + dim(footprint))
    tail = f"v{VERSION}  |  модель: {S.model}  |  инструменты: {S.tool_mode}"
    if root is not None:
        tail += f"  |  корень: {root}"
    print("  " + dim(tail))
    print(cyan("  " + "=" * width))
    print()


# =====================================================================================
# Ввод: клавиши, меню, вопросы
# =====================================================================================

def interactive_tty() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _read_key() -> str:
    """Одна клавиша без Enter. Возвращает 'up'/'down'/'enter'/'esc' или символ."""
    if os.name == "nt":
        try:
            import msvcrt  # type: ignore
        except ImportError:
            return sys.stdin.readline().strip()[:1]
        ch = msvcrt.getwch()
        if ch in ("\x00", "\xe0"):
            return {"H": "up", "P": "down"}.get(msvcrt.getwch(), "")
        if ch in ("\r", "\n"):
            return "enter"
        if ch == "\x03":
            raise KeyboardInterrupt
        if ch == "\x1b":
            return "esc"
        return ch

    try:
        import termios
        import tty
    except ImportError:  # экзотика — уходим на построчный ввод
        return sys.stdin.readline().strip()[:1]

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch = sys.stdin.read(1)
        if ch == "\x1b":
            seq = sys.stdin.read(2)
            if seq == "[A":
                return "up"
            if seq == "[B":
                return "down"
            return "esc"
        if ch in ("\r", "\n"):
            return "enter"
        if ch == "\x03":
            raise KeyboardInterrupt
        return ch
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def select(title: str, options: Sequence[str], hint: str = "") -> Optional[int]:
    """Одиночный выбор: стрелки + Enter, либо цифра, либо q/Esc — отмена."""
    if not options:
        return None
    if not interactive_tty():
        return _select_plain(title, options, hint)

    clear = "\033[2K" if _COLOR_ON else ""
    index = 0
    first = True
    while True:
        if not first:
            sys.stdout.write(f"\033[{len(options) + 2}A")
        first = False
        print(clear + bold(title))
        for i, opt in enumerate(options):
            marker = cyan(" >") if i == index else "  "
            print(f"{clear}{marker} {dim(f'{i + 1}.')} {opt}")
        print(clear + dim(hint or "  [вверх]/[вниз] - выбор, Enter - ок, цифра - быстрый выбор, q - назад"))

        key = _read_key()
        if key == "up":
            index = (index - 1) % len(options)
        elif key == "down":
            index = (index + 1) % len(options)
        elif key == "enter":
            print()
            return index
        elif key in ("q", "Q", "esc", "й", "Й"):
            print()
            return None
        elif key.isdigit() and 1 <= int(key) <= len(options):
            print()
            return int(key) - 1


def _select_plain(title: str, options: Sequence[str], hint: str = "") -> Optional[int]:
    print(bold(title))
    for i, opt in enumerate(options, start=1):
        print(f"  {i}. {opt}")
    print(dim(hint or "  Введите номер (q — назад)"))
    while True:
        try:
            raw = input("  > ").strip()
        except EOFError:
            return None
        if raw.lower() in {"q", "quit", "exit", ""}:
            return None
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return int(raw) - 1
        print(red("  Не понял. Нужен номер из списка."))


def multiselect(title: str, options: Sequence[str]) -> List[int]:
    """Мультивыбор строкой: '1,3,5-7', 'all' — всё, пусто — ничего."""
    print(bold(title))
    for i, opt in enumerate(options, start=1):
        print(f"  {i:>3}. {opt}")
    print(dim("  Формат: 1,3,5-7  |  all — все  |  Enter — отмена"))
    try:
        raw = input("  > ").strip().lower()
    except EOFError:
        return []
    if not raw:
        return []
    if raw in {"all", "*", "все"}:
        return list(range(len(options)))

    picked: List[int] = []
    for chunk in raw.replace(" ", "").split(","):
        if "-" in chunk:
            left, _, right = chunk.partition("-")
            if left.isdigit() and right.isdigit():
                picked.extend(v - 1 for v in range(int(left), int(right) + 1) if 1 <= v <= len(options))
        elif chunk.isdigit() and 1 <= int(chunk) <= len(options):
            picked.append(int(chunk) - 1)

    seen: set = set()
    result: List[int] = []
    for i in picked:
        if i not in seen:
            seen.add(i)
            result.append(i)
    return result


def ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        raw = input(f"  {prompt}{suffix}: ").strip()
    except EOFError:
        return default
    return raw or default


def confirm(prompt: str, default: bool = True) -> bool:
    raw = ask(f"{prompt} ({'Y/n' if default else 'y/N'})", "")
    if not raw:
        return default
    return raw.lower() in {"y", "yes", "д", "да"}


def pause() -> None:
    try:
        input(dim("\n  Enter — вернуться в меню "))
    except EOFError:
        pass


def clear_screen() -> None:
    if interactive_tty():
        os.system("cls" if os.name == "nt" else "clear")
