# -*- coding: utf-8 -*-
"""Точка входа: интерактивное меню и CLI."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from .agent import operation_prompt, prepared, run_agent
from .config import RAW_DIR_NAME, S, WIKI_DIR_NAME
from .convert import choose_backend
from .ops import do_convert, do_doctor, do_ingest, do_init, do_status, version_line
from .report import do_report
from .state import backup_wiki, collect_raw_files, file_fingerprint, is_ingested, load_state
from .ui import (
    ask,
    clear_screen,
    confirm,
    dim,
    green,
    log,
    multiselect,
    pause,
    print_banner,
    red,
    select,
    yellow,
)


# =====================================================================================
# Меню
# =====================================================================================

def _pick_sources(root: Path, state: Dict[str, Any]) -> Optional[List[str]]:
    files = collect_raw_files(root)
    if not files:
        print(yellow(f"В {RAW_DIR_NAME}/ пусто. Положите документы туда."))
        return None
    labels = []
    for rel in files:
        done = is_ingested(state, rel, file_fingerprint(root / rel))
        size_kb = (root / rel).stat().st_size // 1024 or 1
        labels.append(f"{green('[v]') if done else yellow('[ ]')} {rel} "
                      + dim(f"({size_kb} КБ, {choose_backend(Path(rel).suffix)})"))
    picked = multiselect("Выберите источники:", labels)
    return [files[i] for i in picked] if picked else None


def menu_ingest(root: Path) -> None:
    state = load_state(root)
    choice = select(
        "INGEST — загрузка источников в вики",
        [
            "Все новые файлы из raw/ (рекомендуется)",
            "Выбрать файлы вручную",
            "Все файлы заново (--force)",
            "Только конвертация raw/ -> raw_md/, без LLM",
            "Назад",
        ],
    )
    if choice is None or choice == 4:
        return

    args = argparse.Namespace(
        all=False, value=None, force=False, limit=0, stop_on_error=False,
        backup_every=S.backup_every, no_backup=False, targets=None, convert_only=False,
    )

    if choice == 0:
        args.all = True
    elif choice == 1:
        targets = _pick_sources(root, state)
        if not targets:
            return
        args.targets = targets
    elif choice == 2:
        if not confirm("Это перезапишет разбор всех источников. Продолжить?", default=False):
            return
        args.all = args.force = True
    elif choice == 3:
        do_convert(root, force=confirm("Пересобрать уже сконвертированные?", default=False))
        return

    limit = ask("Лимит файлов за прогон (0 — без лимита)", "0")
    args.limit = int(limit) if limit.isdigit() else 0

    system_prompt, tools = prepared(root)
    do_ingest(args, root, system_prompt, tools)


def menu_query(root: Path) -> None:
    question = ask("Вопрос к базе знаний", "")
    if not question:
        return
    system_prompt, tools = prepared(root)
    print(dim("\n  думаю...\n"))
    print(run_agent(system_prompt, operation_prompt("query", question), tools))


def menu_maintenance(root: Path) -> None:
    choice = select(
        "Обслуживание вики",
        ["LINT — проверить целостность", "REINDEX — пересобрать index.md",
         "BACKUP — сделать бэкап wiki/", "Назад"],
    )
    if choice is None or choice == 3:
        return
    if choice == 2:
        dest = backup_wiki(root, "manual")
        print(green(f"Бэкап создан: {dest}") if dest else yellow(f"Каталог {WIKI_DIR_NAME}/ не найден"))
        return

    operation = "lint" if choice == 0 else "reindex"
    system_prompt, tools = prepared(root)
    if operation == "reindex":
        backup_wiki(root, operation)
    print(dim("\n  работаю...\n"))
    print(run_agent(system_prompt, operation_prompt(operation, None), tools))


def menu_report(root: Path) -> None:
    target = ask("Файл отчёта", str(root / "wiki_report.html"))
    do_report(root, target)


def menu_settings() -> None:
    options = ["auto", "cardify", "markitdown", "builtin"]
    while True:
        choice = select(
            "Настройки текущего запуска",
            [
                f"Модель: {S.model}",
                f"Режим инструментов: {S.tool_mode}",
                f"Бэкенд конвертации: {S.convert_backend}",
                f"max_tokens: {S.max_tokens}",
                f"Heartbeat: {S.heartbeat} с",
                "Назад",
            ],
        )
        if choice is None or choice == 5:
            return
        if choice == 0:
            S.model = ask("Имя модели", S.model)
        elif choice == 1:
            index = select("Режим инструментов", ["prompt (рекомендуется)", "native"])
            if index is not None:
                S.tool_mode = "prompt" if index == 0 else "native"
        elif choice == 2:
            index = select("Бэкенд конвертации", options)
            if index is not None:
                S.convert_backend = options[index]
        elif choice == 3:
            raw = ask("max_tokens", str(S.max_tokens))
            if raw.isdigit():
                S.max_tokens = int(raw)
        elif choice == 4:
            raw = ask("Период heartbeat в секундах (0 — выключить)", str(S.heartbeat))
            if raw.isdigit():
                S.heartbeat = int(raw)


def interactive(root: Path) -> int:
    while True:
        clear_screen()
        print_banner(root)
        choice = select(
            "Что делаем?",
            [
                "INGEST   — загрузить документы из raw/ в вики",
                "QUERY    — спросить у базы знаний",
                "STATUS   — что уже обработано",
                "CONVERT  — только конвертация raw/ -> raw_md/",
                "SERVICE  — lint / reindex / backup",
                "REPORT   — HTML-отчёт по базе знаний",
                "DOCTOR   — проверить окружение и конвертеры",
                "INIT     — создать структуру проекта",
                "SETTINGS — модель, режим, конвертер",
                "Выход",
            ],
        )
        if choice is None or choice == 9:
            print(dim("  пока!\n"))
            return 0
        try:
            if choice == 0:
                menu_ingest(root)
            elif choice == 1:
                menu_query(root)
            elif choice == 2:
                do_status(root)
            elif choice == 3:
                do_convert(root, force=confirm("Пересобрать уже сконвертированные?", default=False))
            elif choice == 4:
                menu_maintenance(root)
            elif choice == 5:
                menu_report(root)
            elif choice == 6:
                do_doctor(root)
            elif choice == 7:
                do_init(root)
            elif choice == 8:
                menu_settings()
                continue
        except KeyboardInterrupt:
            print(yellow("\n  прервано"))
        except Exception as exc:
            print(red(f"\n  ОШИБКА: {type(exc).__name__}: {exc}"))
        pause()


# =====================================================================================
# CLI
# =====================================================================================

OPERATIONS = ["menu", "init", "convert", "ingest", "query", "lint", "reindex",
              "backup", "status", "report", "doctor"]


def parse_cli(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="llm_wiki",
        description="LLM-wiki: конвертация любых документов в Markdown + агент базы знаний.",
        epilog="Без аргументов запускается интерактивное меню.",
    )
    parser.add_argument("operation", nargs="?", choices=OPERATIONS, default="menu")
    parser.add_argument("value", nargs="?", help="Путь к raw-файлу для ingest или вопрос для query")
    parser.add_argument("--root", default=os.getenv("LLM_WIKI_ROOT", "."), help="Корень проекта LLM-wiki")
    parser.add_argument("--all", action="store_true", help="INGEST/CONVERT: обработать все файлы из raw/")
    parser.add_argument("--force", action="store_true", help="Обработать заново, даже если файл есть в журнале")
    parser.add_argument("--limit", type=int, default=0, help="Обработать не более N файлов за запуск")
    parser.add_argument("--stop-on-error", action="store_true", help="Прервать прогон при первой ошибке")
    parser.add_argument("--convert-only", action="store_true", help="INGEST: только конвертация, без вызовов LLM")
    parser.add_argument("--backup-every", type=int, default=S.backup_every, help="Бэкап wiki/ каждые N файлов")
    parser.add_argument("--no-backup", action="store_true", help="Отключить автоматические бэкапы")
    parser.add_argument("--model", default=S.model, help=f"Модель для запуска (по умолчанию {S.model})")
    parser.add_argument("--tool-mode", choices=["prompt", "native"], default=S.tool_mode)
    parser.add_argument("--convert-backend", choices=["auto", "cardify", "markitdown", "builtin"],
                        default=S.convert_backend, help="Движок конвертации документов")
    parser.add_argument("--output", "-o", default="", help="REPORT: путь к HTML-файлу отчёта")
    parser.add_argument("--verbose", action="store_true", help="Подробные логи (включая логи cardify)")
    parser.add_argument("--no-banner", action="store_true", help="Не печатать баннер")
    parser.add_argument("--version", action="version", version=version_line())
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_cli(argv)
    S.model = args.model
    S.tool_mode = args.tool_mode
    S.convert_backend = args.convert_backend
    S.verbose = S.verbose or args.verbose
    args.targets = None

    root = Path(args.root).resolve()
    if not root.is_dir():
        log(red(f"Корень проекта не найден: {root}"))
        return 2

    if args.operation == "menu":
        return interactive(root)

    if not args.no_banner:
        print_banner(root)

    if args.operation in {"ingest", "query"} and not args.value and not args.all:
        log(f"Для операции {args.operation} требуется аргумент или --all")
        return 2

    try:
        if args.operation == "init":
            return do_init(root)
        if args.operation == "status":
            return do_status(root)
        if args.operation == "doctor":
            return do_doctor(root)
        if args.operation == "report":
            return do_report(root, args.output)
        if args.operation == "convert":
            targets = None if (args.all or not args.value) else [args.value]
            return do_convert(root, targets, force=args.force)
        if args.operation == "backup":
            dest = backup_wiki(root, "manual")
            if dest is None:
                log(f"Каталог {WIKI_DIR_NAME}/ не найден — бэкапить нечего")
                return 2
            print(f"Бэкап создан: {dest}")
            return 0

        system_prompt, tools = prepared(root)
        if args.operation == "ingest":
            return do_ingest(args, root, system_prompt, tools)

        # query / lint / reindex — бэкап перед возможной записью в wiki/
        if args.operation in {"lint", "reindex"} and not args.no_backup:
            backup_wiki(root, args.operation)
        print(run_agent(system_prompt, operation_prompt(args.operation, args.value), tools))
        return 0
    except KeyboardInterrupt:
        log("\nПрервано пользователем")
        return 130
    except Exception as exc:
        log(red(f"ERROR: {type(exc).__name__}: {exc}"))
        return 1
