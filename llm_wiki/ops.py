# -*- coding: utf-8 -*-
"""Операции LLM-wiki: init, convert, ingest, status, doctor."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .agent import operation_prompt, post_chat, run_agent, WikiTools
from .config import MD_DIR_NAME, RAW_DIR_NAME, S, VERSION, WIKI_DIR_NAME
from .convert import cardify_available, cardify_extensions, choose_backend, ensure_markdown, markitdown_available
from .state import (
    backup_wiki,
    collect_raw_files,
    file_fingerprint,
    is_ingested,
    load_state,
    mark_ingested,
    save_state,
)
from .ui import bold, dim, footprint_intact, green, log, red, render_footprint, yellow

DEFAULT_SCHEMA = f"""# SCHEMA — структура LLM-wiki

Этот файл — контракт для агента. Он задаёт онтологию вики и правила её ведения.
Агент НЕ имеет права переписывать этот файл.

## Каталоги

* `{RAW_DIR_NAME}/`    — сырые источники в любых форматах. Иммутабельны.
* `{MD_DIR_NAME}/` — автоматические Markdown-копии `{RAW_DIR_NAME}/` с YAML-frontmatter. Иммутабельны для агента.
* `{WIKI_DIR_NAME}/`   — производная база знаний. Единственное место записи.

## Типы страниц

| Тип | Каталог | Назначение |
| --- | --- | --- |
| Источник | `{WIKI_DIR_NAME}/sources/` | Одна страница на один документ: о чём он, ключевые факты, ссылки на сущности |
| Сущность | `{WIKI_DIR_NAME}/entities/` | Процесс, система, подразделение, роль, риск, контроль |
| Концепт  | `{WIKI_DIR_NAME}/concepts/` | Термин или метод, встречающийся в нескольких источниках |

## Обязательные файлы

* `{WIKI_DIR_NAME}/index.md` — единая карта вики: по одной таблице на тип страниц, по строке на страницу.
* `{WIKI_DIR_NAME}/log.md`   — append-only журнал: дата, источник, что добавлено/изменено.

## Правила

1. Идентичность страницы = имя файла. Одна сущность — одна страница, дубликаты запрещены.
2. Ссылки между страницами — относительные markdown-ссылки.
3. У каждой страницы-источника в шапке указывается оригинальный путь из `{RAW_DIR_NAME}/`.
4. Противоречия между источниками не «сглаживаются»: фиксируются оба утверждения со ссылками.
5. Новая страница создаётся, только если тема встречается в двух и более контекстах
   либо явно является самостоятельной сущностью домена.
"""

DEFAULT_INDEX = """# Индекс базы знаний

## Источники

| Страница | Источник | Кратко |
| --- | --- | --- |

## Сущности

| Страница | Тип | Кратко |
| --- | --- | --- |

## Концепты

| Страница | Кратко |
| --- | --- |
"""


def do_init(root: Path) -> int:
    for name in (RAW_DIR_NAME, MD_DIR_NAME, WIKI_DIR_NAME,
                 f"{WIKI_DIR_NAME}/sources", f"{WIKI_DIR_NAME}/entities", f"{WIKI_DIR_NAME}/concepts"):
        (root / name).mkdir(parents=True, exist_ok=True)

    created: List[str] = []
    if not any((root / n).is_file() for n in ("SCHEMA.md", "CLAUDE.md", "AGENTS.md")):
        (root / "SCHEMA.md").write_text(DEFAULT_SCHEMA, encoding="utf-8")
        created.append("SCHEMA.md")
    index = root / WIKI_DIR_NAME / "index.md"
    if not index.exists():
        index.write_text(DEFAULT_INDEX, encoding="utf-8")
        created.append(f"{WIKI_DIR_NAME}/index.md")
    log_file = root / WIKI_DIR_NAME / "log.md"
    if not log_file.exists():
        log_file.write_text("# Журнал изменений\n\n", encoding="utf-8")
        created.append(f"{WIKI_DIR_NAME}/log.md")

    print(green(f"Структура готова в {root}"))
    if created:
        print("Созданы файлы: " + ", ".join(created))
    print(dim(f"Положите документы в {root / RAW_DIR_NAME} и запустите ingest."))
    return 0


def do_convert(root: Path, targets: Optional[List[str]] = None, force: bool = False) -> int:
    state = load_state(root)
    candidates = targets if targets is not None else collect_raw_files(root)
    if not candidates:
        print(yellow(f"В {RAW_DIR_NAME}/ нет файлов"))
        return 2

    ok = cached = 0
    failed: List[str] = []
    for index, rel in enumerate(candidates, start=1):
        report = ensure_markdown(root, rel, state, force=force)
        if report.get("ok"):
            ok += 1
            if report.get("cached"):
                cached += 1
                print(dim(f"  [{index}/{len(candidates)}] {rel} — уже сконвертирован"))
            else:
                print(f"  [{index}/{len(candidates)}] {green('OK')} {rel} -> {report['markdown']} "
                      + dim(f"({report['backend']}, {report.get('chars', 0)} симв.)"))
            for warning in report.get("warnings", []) or []:
                print(yellow(f"        ! {warning}"))
        else:
            failed.append(rel)
            print(f"  [{index}/{len(candidates)}] {red('FAIL')} {rel}")
            for error in report.get("errors", []):
                print(red(f"        {error}"))

    save_state(root, state)
    print(f"\nКонвертировано: {ok} (из них из кэша {cached}), ошибок: {len(failed)}")
    return 1 if failed else 0


def do_ingest(args: argparse.Namespace, root: Path, system_prompt: str, tools: WikiTools) -> int:
    state = load_state(root)
    backups_on = not args.no_backup

    if getattr(args, "targets", None):
        candidates = list(args.targets)
    elif args.all:
        candidates = collect_raw_files(root)
        if not candidates:
            log(f"В каталоге {RAW_DIR_NAME}/ нет файлов для обработки")
            return 2
    else:
        raw_path = (root / args.value).resolve()
        raw_root = (root / RAW_DIR_NAME).resolve()
        try:
            raw_path.relative_to(raw_root)
        except ValueError:
            log(f"INGEST разрешён только для файла внутри {RAW_DIR_NAME}/")
            return 2
        if not raw_path.is_file():
            log(f"Источник не найден: {args.value}")
            return 2
        candidates = [raw_path.relative_to(root).as_posix()]

    pending: List[Tuple[str, Dict[str, Any]]] = []
    skipped = 0
    for rel in candidates:
        fingerprint = file_fingerprint(root / rel)
        if not args.force and is_ingested(state, rel, fingerprint):
            skipped += 1
            continue
        pending.append((rel, fingerprint))

    if args.limit and len(pending) > args.limit:
        pending = pending[: args.limit]

    print(f"Файлов в очереди: {len(pending)} | уже обработано ранее: {skipped} | всего: {len(candidates)}")
    print(f"Модель: {S.model} | max_tokens: {S.max_tokens} | режим инструментов: {S.tool_mode} "
          f"| конвертер: {S.convert_backend}")
    if not pending:
        print("Нечего обрабатывать. Для повторного прогона используйте --force")
        return 0

    if backups_on:
        backup_wiki(root, "start")

    processed = 0
    failed: List[str] = []
    convert_failed: List[str] = []

    for index, (rel, fingerprint) in enumerate(pending, start=1):
        print(bold(f"\n--- INGEST [{index}/{len(pending)}]: {rel} ---"), flush=True)

        # ЭТАП 1. Конвертация источника в Markdown.
        try:
            report = ensure_markdown(root, rel, state, force=args.force)
        except Exception as exc:
            report = {"ok": False, "errors": [f"{type(exc).__name__}: {exc}"]}
        if not report.get("ok"):
            convert_failed.append(rel)
            log(red(f"КОНВЕРТАЦИЯ НЕ УДАЛАСЬ: {rel}"))
            for error in report.get("errors", []):
                log(f"    {error}")
            save_state(root, state)
            if args.stop_on_error:
                break
            continue

        save_state(root, state)
        md_rel = report["markdown"]
        print(f"  конвертация: {md_rel} "
              + dim("из кэша" if report.get("cached") else str(report.get("backend", ""))))
        for warning in report.get("warnings", []) or []:
            print(yellow(f"  ! {warning}"))

        if getattr(args, "convert_only", False):
            processed += 1
            continue

        # ЭТАП 2. Агент раскладывает markdown по вики.
        try:
            answer = run_agent(system_prompt, operation_prompt("ingest", rel, md_rel), tools)
        except KeyboardInterrupt:
            log("\nПрервано пользователем. Журнал сохранён, повторный запуск продолжит с этого файла.")
            save_state(root, state)
            return 130
        except Exception as exc:
            failed.append(rel)
            log(red(f"ОШИБКА на {rel}: {type(exc).__name__}: {exc}"))
            if args.stop_on_error:
                save_state(root, state)
                break
            continue

        print(answer)
        mark_ingested(state, rel, fingerprint)
        save_state(root, state)
        processed += 1

        if backups_on and args.backup_every > 0 and processed % args.backup_every == 0:
            backup_wiki(root, f"after-{processed}")

    if backups_on and processed and not getattr(args, "convert_only", False):
        backup_wiki(root, "final")

    print(f"\nИтог: обработано {processed}, ошибок агента {len(failed)}, "
          f"ошибок конвертации {len(convert_failed)}, пропущено ранее {skipped}")
    for rel in failed + convert_failed:
        print(f"  - {rel}")
    return 1 if (failed or convert_failed) else 0


def do_status(root: Path) -> int:
    state = load_state(root)
    if not (root / RAW_DIR_NAME).is_dir():
        print(yellow(f"Каталог {RAW_DIR_NAME}/ не найден. Запустите init."))
        return 2

    candidates = collect_raw_files(root)
    done, pending = [], []
    for rel in candidates:
        (done if is_ingested(state, rel, file_fingerprint(root / rel)) else pending).append(rel)

    by_ext: Dict[str, int] = {}
    for rel in candidates:
        ext = Path(rel).suffix.lower() or "(без расширения)"
        by_ext[ext] = by_ext.get(ext, 0) + 1

    print(f"Обработано: {green(str(len(done)))} | В очереди: {yellow(str(len(pending)))} "
          f"| Всего в {RAW_DIR_NAME}/: {len(candidates)}")
    print("Форматы: " + ", ".join(f"{k} x{v}" for k, v in sorted(by_ext.items())))
    print(f"Конвертировано в {MD_DIR_NAME}/: {len(state.get('converted', {}))}")

    for rel in pending[:50]:
        print(f"  ожидает: {rel} " + dim(f"[{choose_backend(Path(rel).suffix)}]"))
    if len(pending) > 50:
        print(dim(f"  ... и ещё {len(pending) - 50}"))

    wiki = root / WIKI_DIR_NAME
    if wiki.is_dir():
        print(f"Страниц в {WIKI_DIR_NAME}/: {len(list(wiki.rglob('*.md')))}")
    backups = root / S.backup_dir
    if backups.is_dir():
        items = sorted(p.name for p in backups.iterdir() if p.is_dir())
        print(f"Бэкапов: {len(items)}" + (f", последний: {items[-1]}" if items else ""))
    return 0


def do_doctor(root: Path) -> int:
    print(bold("Окружение"))
    print(f"  python           : {sys.version.split()[0]}")
    print(f"  корень проекта   : {root}  " + (green("ok") if root.is_dir() else red("нет")))
    for name in (RAW_DIR_NAME, MD_DIR_NAME, WIKI_DIR_NAME):
        print(f"  {name:<17}: " + (green("есть") if (root / name).is_dir() else yellow("нет — запустите init")))
    schema_found = any((root / n).is_file() for n in ("SCHEMA.md", "CLAUDE.md", "AGENTS.md"))
    print("  SCHEMA.md        : " + (green("есть") if schema_found else red("нет")))
    print("  сборка           : " + (dim(render_footprint()) if footprint_intact() else yellow("подпись изменена")))

    print(bold("\nGigaChat"))
    print("  GIGACHAT_API_URL : " + (green(S.api_url) if S.api_url else red("не задан")))
    print("  JPY_API_TOKEN    : " + (green("задан") if S.token else red("не задан")))
    print(f"  модель           : {S.model}")
    print(f"  режим инструментов: {S.tool_mode}" + dim("  (рекомендуется prompt)"))
    print(f"  max_tokens       : {S.max_tokens} | таймаут: {S.timeout} с | verify_ssl: {S.verify_ssl}")
    print(f"  heartbeat        : каждые {S.heartbeat} с" if S.heartbeat else "  heartbeat        : выключен")

    print(bold("\nКонвертеры"))
    ok_cardify, info_cardify = cardify_available()
    print("  cardify          : " + (green(info_cardify) if ok_cardify else yellow(f"нет ({info_cardify})")))
    if ok_cardify:
        print(dim("    расширения: " + ", ".join(cardify_extensions())))
    ok_md, info_md = markitdown_available()
    print("  markitdown       : " + (green(info_md) if ok_md else yellow(f"нет ({info_md})")))
    for module, label in (("docx", "python-docx (.docx)"), ("pptx", "python-pptx (.pptx)"),
                          ("openpyxl", "openpyxl (.xlsx)"), ("pdfplumber", "pdfplumber (.pdf)"),
                          ("pypdf", "pypdf (.pdf, резерв)")):
        try:
            __import__(module)
            print(f"  {label:<17}: " + green("есть"))
        except ImportError:
            print(f"  {label:<17}: " + yellow("нет"))
    print(dim("  stdlib-форматы всегда доступны: .md .txt .csv .tsv .json .yaml .ipynb .html .eml .py .sql"))

    print(bold("\nПроверка связи с API"))
    if not (S.api_url and S.token):
        print(yellow("  пропущено: не заданы переменные окружения"))
        return 0
    try:
        data = post_chat(
            {"model": S.model, "messages": [{"role": "user", "content": "ping"}],
             "max_tokens": 16, "temperature": 0.01, "n": 1},
            label="ping",
        )
        answer = (data["choices"][0]["message"].get("content") or "").strip()
        print(green(f"  ok: модель ответила ({answer[:60]!r})"))
    except Exception as exc:
        print(red(f"  ошибка: {type(exc).__name__}: {exc}"))
        return 1
    return 0


def version_line() -> str:
    return f"llm_wiki {VERSION}  |  {render_footprint()}"
