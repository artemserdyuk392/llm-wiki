# -*- coding: utf-8 -*-
"""Агент: файловые инструменты, промпты, обращения к GigaChat и циклы шагов.

Два протокола инструментов:

* ``prompt`` (по умолчанию) — модель возвращает JSON-действие текстом ответа;
* ``native`` — штатный function calling шлюза.

Каждое обращение к API сопровождается heartbeat'ом: раз в N секунд в stderr
уходит строка с накопленным временем ожидания, чтобы долгую генерацию нельзя
было спутать с зависанием.
"""

from __future__ import annotations

import json
import os
import random
import re
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

from .config import IGNORED_NAMES, MD_DIR_NAME, RAW_DIR_NAME, RETRY_STATUSES, S, TEXT_EXTENSIONS, WIKI_DIR_NAME
from .convert import read_text_any_encoding
from .ui import dim, log, red, yellow


class ApiError(RuntimeError):
    pass


class EmptyResponseError(RuntimeError):
    pass


# =====================================================================================
# Файловые инструменты модели
# =====================================================================================

class WikiTools:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def _safe_path(self, relative_path: str) -> Path:
        candidate = self.root if relative_path in ("", ".") else (self.root / relative_path).resolve()
        try:
            common = os.path.commonpath([str(self.root), str(candidate)])
        except ValueError as exc:
            raise ValueError("Некорректный путь") from exc
        if common != str(self.root):
            raise ValueError("Доступ за пределы корня wiki запрещён")
        return candidate

    def list_files(self, path: str = ".") -> Dict[str, Any]:
        target = self._safe_path(path)
        if not target.exists():
            raise FileNotFoundError(f"Путь не найден: {path}")
        if not target.is_dir():
            raise NotADirectoryError(f"Это не каталог: {path}")

        entries: List[Dict[str, Any]] = []
        for item in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
            if item.name in IGNORED_NAMES or item.name.startswith("._"):
                continue
            entries.append(
                {
                    "path": item.relative_to(self.root).as_posix(),
                    "type": "directory" if item.is_dir() else "file",
                    "size": None if item.is_dir() else item.stat().st_size,
                }
            )
        return {"path": path, "entries": entries}

    def read_file(self, path: str, start_line: int = 1, max_lines: int = 400) -> Dict[str, Any]:
        target = self._safe_path(path)
        if not target.exists():
            raise FileNotFoundError(f"Файл не найден: {path}")
        if not target.is_file():
            raise IsADirectoryError(f"Это каталог, а не файл: {path}")
        if target.suffix.lower() not in TEXT_EXTENSIONS:
            raise ValueError(
                "Бинарные источники напрямую не читаются. Markdown-версия любого файла "
                f"из {RAW_DIR_NAME}/ лежит в {MD_DIR_NAME}/ с тем же путём и суффиксом .md. "
                f"Разрешённые форматы чтения: {sorted(TEXT_EXTENSIONS)}"
            )
        if start_line < 1 or max_lines < 1:
            raise ValueError("start_line и max_lines должны быть положительными")
        max_lines = min(max_lines, 1000)

        lines = read_text_any_encoding(target)[0].splitlines()
        selected = lines[start_line - 1 : start_line - 1 + max_lines]
        content = "\n".join(selected)
        truncated_by_chars = len(content) > S.max_read_chars
        if truncated_by_chars:
            content = content[: S.max_read_chars]

        end_line = start_line + len(selected) - 1 if selected else start_line - 1
        return {
            "path": path,
            "start_line": start_line,
            "end_line": end_line,
            "total_lines": len(lines),
            "has_more": end_line < len(lines) or truncated_by_chars,
            "content": content,
        }

    def write_file(self, path: str, content: str, mode: str = "overwrite") -> Dict[str, Any]:
        target = self._safe_path(path)
        rel = target.relative_to(self.root).as_posix()

        if rel == RAW_DIR_NAME or rel.startswith(RAW_DIR_NAME + "/"):
            raise PermissionError(f"Каталог {RAW_DIR_NAME}/ иммутабелен: запись запрещена")
        if rel == MD_DIR_NAME or rel.startswith(MD_DIR_NAME + "/"):
            raise PermissionError(f"Каталог {MD_DIR_NAME}/ генерируется конвертером и иммутабелен для агента")
        if not (rel == WIKI_DIR_NAME or rel.startswith(WIKI_DIR_NAME + "/")):
            raise PermissionError(f"Запись разрешена только в {WIKI_DIR_NAME}/")
        if target.suffix.lower() != ".md":
            raise ValueError("Записываются только Markdown-файлы (.md)")
        if mode not in {"overwrite", "append"}:
            raise ValueError("mode должен быть overwrite или append")

        # Защита от «модель обнулила index.md»: полная перезапись существующего файла
        # почти пустым содержимым отклоняется, файл остаётся прежним.
        if mode == "overwrite" and target.is_file():
            old_size = len(target.read_text(encoding="utf-8").strip())
            new_size = len(content.strip())
            if old_size > 200 and new_size < old_size // 4:
                raise ValueError(
                    "Перезапись отклонена: новое содержимое ({0} симв.) радикально короче "
                    "текущего ({1} симв.). Прочитай файл целиком и сохрани существующие "
                    "разделы, либо используй mode=append.".format(new_size, old_size)
                )

        target.parent.mkdir(parents=True, exist_ok=True)
        if mode == "append":
            with target.open("a", encoding="utf-8") as file:
                file.write(content)
        else:  # атомарная замена: незавершённая запись не повредит существующий файл
            fd, temp_name = tempfile.mkstemp(prefix=target.name + ".", dir=str(target.parent))
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as file:
                    file.write(content)
                os.replace(temp_name, target)
            finally:
                if os.path.exists(temp_name):
                    os.unlink(temp_name)

        return {"status": "ok", "path": rel, "mode": mode, "chars_written": len(content)}

    def execute(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        try:
            if name == "list_files":
                result = self.list_files(**arguments)
            elif name == "read_file":
                result = self.read_file(**arguments)
            elif name == "write_file":
                result = self.write_file(**arguments)
            else:
                raise ValueError(f"Неизвестный инструмент: {name}")
            return {"ok": True, "result": result}
        except Exception as exc:  # ошибка инструмента возвращается модели, а не роняет цикл
            return {"ok": False, "error": type(exc).__name__, "message": str(exc)}


FUNCTIONS: List[Dict[str, Any]] = [
    {
        "name": "list_files",
        "description": (
            "Показывает непосредственное содержимое каталога внутри LLM-wiki. "
            f"Используй для навигации по {MD_DIR_NAME}/ и {WIKI_DIR_NAME}/."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Относительный путь от корня проекта, например '.', 'raw_md' или 'wiki/concepts'.",
                }
            },
        },
    },
    {
        "name": "read_file",
        "description": (
            "Читает UTF-8 текстовый файл внутри LLM-wiki с нумерацией диапазона строк. "
            "Для длинного файла вызывай повторно со следующим start_line."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Относительный путь к файлу, например 'wiki/index.md'."},
                "start_line": {"type": "integer", "description": "Первая строка, начиная с 1. По умолчанию 1."},
                "max_lines": {"type": "integer", "description": "Максимум строк за чтение. По умолчанию 400, максимум 1000."},
            },
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": (
            "Создаёт, полностью перезаписывает или дописывает Markdown-файл в wiki/. "
            "Запись в raw/, raw_md/ и за пределы wiki/ запрещена программно. "
            "Перед overwrite существующего файла сначала прочитай его целиком и сохрани все "
            "существующие разделы: перезапись, радикально сокращающая файл, отклоняется."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Относительный путь только внутри wiki/."},
                "content": {"type": "string", "description": "Полное содержимое файла или добавляемый фрагмент."},
                "mode": {
                    "type": "string",
                    "enum": ["overwrite", "append"],
                    "description": "overwrite — полная атомарная замена; append — дописать в конец.",
                },
            },
            "required": ["path", "content", "mode"],
        },
    },
]


PROMPT_TOOL_PROTOCOL = """
У тебя нет прямого доступа к файловой системе. Для каждого следующего действия верни РОВНО
один JSON-объект без markdown и без пояснений:

Вызов инструмента:
{"tool":"list_files","arguments":{"path":"wiki"}}
{"tool":"read_file","arguments":{"path":"wiki/index.md","start_line":1,"max_lines":400}}
{"tool":"write_file","arguments":{"path":"wiki/example.md","content":"...","mode":"overwrite"}}

Завершение:
{"final":"Итоговый ответ пользователю"}

Внутри content экранируй переводы строк как \\n и кавычки как \\", иначе JSON не разберётся.
Не объединяй несколько вызовов в одном ответе. После каждого результата решай следующий шаг.
""".strip()


def load_schema(root: Path) -> str:
    for name in ("SCHEMA.md", "CLAUDE.md", "AGENTS.md"):
        path = root / name
        if path.is_file():
            return path.read_text(encoding="utf-8")
    raise FileNotFoundError(
        "Не найден SCHEMA.md, CLAUDE.md или AGENTS.md в корне проекта. Создайте структуру: init"
    )


def build_system_prompt(schema: str, tool_mode: str) -> str:
    prompt = f"""
Ты — файловый агент, обслуживающий локальную LLM-wiki.
Выполняй задачу самостоятельно до завершения, последовательно используя доступные инструменты.
За один ответ делай РОВНО ОДИН вызов инструмента. Никогда не объединяй несколько вызовов в
одном ответе: дождись результата и только потом решай следующий шаг.
Не выдумывай содержимое файлов и не утверждай, что файл изменён, пока write_file не вернул ok=true.
Не пытайся обращаться к интернету, shell, Python, базам данных или неописанным инструментам.

СТРОГОЕ ПРАВИЛО ПРОТОКОЛА: за один ответ делай РОВНО ОДИН вызов инструмента.
Никогда не объединяй несколько вызовов в одном ответе — шлюз этого не поддерживает
и вернёт ошибку. Дождись результата вызова и только затем решай следующий шаг.

Структура каталогов:
* {RAW_DIR_NAME}/     — сырые источники в исходных форматах (docx, pdf, pptx, xlsx, ...).
             Источник истины, никогда не изменяются и напрямую не читаются.
* {MD_DIR_NAME}/  — автоматически сгенерированные Markdown-версии файлов из {RAW_DIR_NAME}/
             с YAML-frontmatter (поля source, format, converted_by). Именно их ты читаешь
             при INGEST. Не изменяй их.
* {WIKI_DIR_NAME}/    — производная база знаний. Единственный каталог, доступный на запись.

В ссылках на источник внутри {WIKI_DIR_NAME}/ указывай ОРИГИНАЛЬНЫЙ путь из поля source во
frontmatter (например {RAW_DIR_NAME}/reports/q1.docx), а не путь markdown-копии.
При поиске по wiki сначала читай wiki/index.md, затем только релевантные страницы.
При перезаписи существующего файла сначала прочитай его целиком и сохрани все существующие
разделы: overwrite означает «дополненная полная версия», а не «только новый фрагмент».
wiki/index.md — ЕДИНАЯ карта вики с постоянным набором разделов и таблиц. Добавляя страницы,
прочитай index.md целиком и перезапиши его через write_file с mode=overwrite, вставив новые
строки в уже существующие таблицы по типу страницы (источники, сущности, концепты).
НИКОГДА не используй append для index.md и не создавай в нём разделы вида
«Новые страницы (doc_NNN)» — новые строки идут в общие таблицы.
Append-only ведётся только wiki/log.md.

Ниже обязательная схема проекта:

--- BEGIN SCHEMA ---
{schema}
--- END SCHEMA ---
""".strip()
    if tool_mode == "prompt":
        prompt += "\n\n" + PROMPT_TOOL_PROTOCOL
    return prompt


def operation_prompt(operation: str, value: Optional[str], md_path: Optional[str] = None) -> str:
    if operation == "ingest":
        assert value is not None
        return (
            f"Выполни процедуру INGEST для источника '{value}'. "
            f"Его Markdown-представление лежит в '{md_path or value}' — читай именно этот файл "
            "(при необходимости несколькими вызовами read_file подряд). "
            "Первые строки файла — YAML-frontmatter с провенансом источника. "
            "Обнови необходимые страницы wiki, index.md и append-only log.md. "
            f"В ссылках указывай оригинальный путь '{value}'. "
            f"Не изменяй {RAW_DIR_NAME}/ и {MD_DIR_NAME}/. "
            "В конце кратко перечисли фактически изменённые файлы."
        )
    if operation == "query":
        assert value is not None
        return (
            "Выполни процедуру QUERY. Сначала прочитай wiki/index.md, затем релевантные страницы. "
            f"Вопрос пользователя: {value}\n"
            "Ответь по содержимому wiki с указанием использованных файлов. "
            "Не создавай новую страницу без явной необходимости для повторного использования результата."
        )
    if operation == "reindex":
        return (
            "Пересобери wiki/index.md как единую карту вики. "
            "Обойди каталоги wiki/ и прочитай index.md целиком. "
            "Собери ОДНУ таблицу на каждый тип страниц (базовые, источники, сущности, концепты), "
            "по одной строке на страницу, без дублей. "
            "Убери разделы вида «Новые страницы (doc_NNN)», перенеся их строки в общие таблицы. "
            "Запиши результат одним write_file с mode=overwrite. "
            "Не изменяй никакие другие файлы, кроме wiki/index.md."
        )
    return (
        "Выполни процедуру LINT для всей wiki. Проверь битые wiki-ссылки, сироты, дубликаты, "
        f"пропуски в index.md, нарушения иммутабельности {RAW_DIR_NAME}/ и очевидные внутренние "
        "противоречия. Не исправляй спорные содержательные выводы автоматически. "
        "Верни компактный отчёт с приоритетами."
    )


# =====================================================================================
# Транспорт
# =====================================================================================

def require_environment() -> None:
    missing = []
    if not S.api_url:
        missing.append("GIGACHAT_API_URL")
    if not S.token:
        missing.append("JPY_API_TOKEN")
    if missing:
        raise RuntimeError("Не заданы переменные окружения: " + ", ".join(missing))


class Heartbeat:
    """Пока ждём ответ модели, раз в N секунд печатает, сколько уже ждём.

    Нужен, чтобы долгая генерация (glm-5.2 умеет думать минутами) визуально
    отличалась от зависшего процесса.
    """

    def __init__(self, label: str) -> None:
        self.label = label
        self.started = time.monotonic()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def __enter__(self) -> "Heartbeat":
        if S.heartbeat > 0:
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
        return self

    def _run(self) -> None:
        while not self._stop.wait(S.heartbeat):
            elapsed = time.monotonic() - self.started
            sys.stderr.write(dim(f"  ... {self.label}: жду ответ модели {elapsed:.0f} c\n"))
            sys.stderr.flush()

    def __exit__(self, *exc: Any) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=0.2)

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started


_last_call_at = 0.0


def _throttle() -> None:
    global _last_call_at
    if S.sleep_between_calls > 0 and _last_call_at:
        wait = S.sleep_between_calls - (time.monotonic() - _last_call_at)
        if wait > 0:
            time.sleep(wait)
    _last_call_at = time.monotonic()


def _retry_delay(attempt: int) -> float:
    delay = min(S.retry_base_delay * (2 ** (attempt - 1)), S.retry_max_delay)
    return delay + random.uniform(0, delay * 0.25)  # джиттер, чтобы не долбить в такт


def post_chat(payload: Dict[str, Any], label: str = "") -> Dict[str, Any]:
    """POST /chat/completions с ретраями на 5xx, сетевые сбои и битый ответ."""
    headers = {
        "Authorization": f"Bearer {S.token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    last_error = "неизвестная ошибка"

    for attempt in range(1, S.max_attempts + 1):
        _throttle()
        with Heartbeat(label or "запрос") as beat:
            try:
                response = requests.post(
                    f"{S.api_url}/chat/completions",
                    headers=headers,
                    json=payload,
                    timeout=S.timeout,
                    verify=S.verify_ssl,
                )
            except requests.RequestException as exc:
                last_error = f"сеть/таймаут: {type(exc).__name__}: {exc}"
                response = None

        if response is not None:
            if response.ok:
                try:
                    data = response.json()
                except ValueError:
                    last_error = f"ответ не JSON: {response.text[:300]}"
                else:
                    if data.get("choices"):
                        if S.verbose:
                            log(dim(f"  [api] {label} ответ за {beat.elapsed:.1f} c"))
                        return data
                    last_error = f"пустой choices: {json.dumps(data, ensure_ascii=False)[:300]}"
            elif response.status_code in RETRY_STATUSES:
                last_error = f"HTTP {response.status_code}: {response.text[:300]}"
            else:
                raise ApiError(f"GigaChat API {response.status_code}: {response.text}")

        if attempt < S.max_attempts:
            delay = _retry_delay(attempt)
            log(yellow(f"  [retry {attempt}/{S.max_attempts - 1}] {last_error} — пауза {delay:.1f} с"))
            time.sleep(delay)

    raise ApiError(f"API недоступен после {S.max_attempts} попыток. Последняя ошибка: {last_error}")


# =====================================================================================
# Циклы агента
# =====================================================================================

CONTINUE_NUDGE = (
    "Ты вернул пустой ответ. Продолжи выполнение задачи: сделай следующий вызов инструмента "
    "или выдай итоговый текстовый ответ."
)

TRUNCATED_NUDGE = (
    "Твой предыдущий ответ оборвался по лимиту токенов, поэтому он не был применён. "
    "Сделай следующий шаг компактнее: пиши страницу частями — сначала write_file с mode=overwrite "
    "и первой частью, затем write_file с mode=append для остальных. Не рассуждай подробно."
)


def parse_arguments(raw: Any) -> Dict[str, Any]:
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise ValueError("Аргументы функции должны быть JSON-объектом")
        return parsed
    raise ValueError("Неподдерживаемый формат аргументов функции")


def extract_json_object(text: str) -> Dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise ValueError(f"Модель не вернула JSON-действие: {text[:500]}")
        candidate = text[start : end + 1]
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            # Частый брак моделей: сырые переводы строк внутри строкового литерала.
            value = json.loads(candidate, strict=False)
    if not isinstance(value, dict):
        raise ValueError("Ответ протокола инструментов должен быть JSON-объектом")
    return value


def _tool_log(step: int, name: str, arguments: Dict[str, Any], result: Dict[str, Any]) -> None:
    preview = ", ".join(str(v)[:40].replace("\n", " ") for v in arguments.values())
    status = "ok" if result.get("ok") else red("FAIL")
    log(dim(f"  [{step:>3}] ") + f"{name}({preview}) -> {status}")
    if not result.get("ok"):
        log(red(f"        {result.get('error')}: {str(result.get('message'))[:200]}"))


def run_native_agent(system_prompt: str, user_prompt: str, tools: WikiTools) -> str:
    messages: List[Dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    empty_streak = truncated_streak = 0

    for step in range(1, S.max_steps + 1):
        data = post_chat(
            {
                "model": S.model,
                "messages": messages,
                "functions": FUNCTIONS,
                "function_call": "auto",
                "temperature": 0.01,
                "max_tokens": S.max_tokens,
                "n": 1,
            },
            label=f"шаг {step}/{S.max_steps}",
        )
        choice = data["choices"][0]
        message = choice["message"]

        # Обрыв по лимиту токенов: аргументы функции почти наверняка обрезаны,
        # выполнять такой вызов нельзя — именно так в wiki попадает огрызок страницы.
        if choice.get("finish_reason") == "length":
            truncated_streak += 1
            if truncated_streak > S.truncated_retries:
                raise RuntimeError(
                    f"Генерация обрывается по лимиту токенов {truncated_streak} раз подряд. "
                    f"Увеличь GIGACHAT_MAX_TOKENS (сейчас {S.max_tokens}) или смени модель."
                )
            log(yellow(f"  [truncated {truncated_streak}/{S.truncated_retries}] ответ оборван по лимиту — повтор"))
            messages.append({"role": "user", "content": TRUNCATED_NUDGE})
            continue
        truncated_streak = 0

        function_call = message.get("function_call")
        if function_call:
            empty_streak = 0
            name = function_call.get("name", "")
            try:
                arguments = parse_arguments(function_call.get("arguments"))
                result = tools.execute(name, arguments)
            except Exception as exc:
                arguments, result = {}, {"ok": False, "error": type(exc).__name__, "message": str(exc)}
            _tool_log(step, name, arguments, result)

            messages.append(message)  # включая functions_state_id
            messages.append({"role": "function", "name": name, "content": json.dumps(result, ensure_ascii=False)})
            continue

        content = (message.get("content") or "").strip()
        if content:
            return content

        empty_streak += 1
        if empty_streak > S.empty_retries:
            raise EmptyResponseError(f"Модель вернула пустой ответ {empty_streak} раз подряд")
        log(yellow(f"  [empty {empty_streak}/{S.empty_retries}] пустой ответ модели — повтор"))
        time.sleep(_retry_delay(empty_streak))
        if empty_streak >= 2:
            messages.append({"role": "user", "content": CONTINUE_NUDGE})

    raise RuntimeError(f"Превышен лимит шагов агента: {S.max_steps}")


def run_prompt_agent(system_prompt: str, user_prompt: str, tools: WikiTools) -> str:
    """Основной режим на внутреннем шлюзе: действие приходит JSON-ом в тексте ответа."""
    messages: List[Dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    bad_streak = truncated_streak = 0

    for step in range(1, S.max_steps + 1):
        data = post_chat(
            {
                "model": S.model,
                "messages": messages,
                "temperature": 0.01,
                "max_tokens": S.max_tokens,
                "n": 1,
            },
            label=f"шаг {step}/{S.max_steps}",
        )
        choice = data["choices"][0]
        content = (choice["message"].get("content") or "").strip()

        if choice.get("finish_reason") == "length":
            truncated_streak += 1
            if truncated_streak > S.truncated_retries:
                raise RuntimeError(
                    f"Генерация обрывается по лимиту токенов {truncated_streak} раз подряд. "
                    f"Увеличь GIGACHAT_MAX_TOKENS (сейчас {S.max_tokens}) или смени модель."
                )
            log(yellow(f"  [truncated {truncated_streak}/{S.truncated_retries}] ответ оборван по лимиту — повтор"))
            messages.append({"role": "user", "content": TRUNCATED_NUDGE})
            continue
        truncated_streak = 0

        if not content:
            bad_streak += 1
            if bad_streak > S.empty_retries:
                raise EmptyResponseError(f"Модель вернула пустой ответ {bad_streak} раз подряд")
            log(yellow(f"  [empty {bad_streak}/{S.empty_retries}] пустой ответ модели — повтор"))
            time.sleep(_retry_delay(bad_streak))
            continue

        try:
            action = extract_json_object(content)
        except (ValueError, json.JSONDecodeError) as exc:
            bad_streak += 1
            if bad_streak > S.empty_retries:
                raise
            log(yellow(f"  [bad json {bad_streak}/{S.empty_retries}] {exc}"))
            messages.append({"role": "assistant", "content": content})
            messages.append(
                {
                    "role": "user",
                    "content": "Ответ не распознан как JSON-действие. Верни РОВНО один JSON-объект "
                    "вида {\"tool\":...,\"arguments\":{...}} или {\"final\":\"...\"}. "
                    "Экранируй переводы строк внутри content как \\n.",
                }
            )
            continue

        bad_streak = 0
        if "final" in action:
            return str(action["final"])
        if "tool" not in action:
            raise ValueError(f"Нет поля tool или final: {action}")

        name = str(action["tool"])
        arguments = action.get("arguments") or {}
        if not isinstance(arguments, dict):
            raise ValueError("arguments должен быть JSON-объектом")
        result = tools.execute(name, arguments)
        _tool_log(step, name, arguments, result)

        messages.append({"role": "assistant", "content": content})
        messages.append({"role": "user", "content": "TOOL_RESULT " + json.dumps(result, ensure_ascii=False)})

    raise RuntimeError(f"Превышен лимит шагов агента: {S.max_steps}")


def run_agent(system_prompt: str, prompt: str, tools: WikiTools) -> str:
    if S.tool_mode == "native":
        return run_native_agent(system_prompt, prompt, tools)
    if S.tool_mode == "prompt":
        return run_prompt_agent(system_prompt, prompt, tools)
    raise ValueError("Режим инструментов должен быть native или prompt")


def prepared(root: Path) -> "tuple[str, WikiTools]":
    """Проверить окружение и собрать системный промпт + инструменты."""
    require_environment()
    return build_system_prompt(load_schema(root), S.tool_mode), WikiTools(root)
