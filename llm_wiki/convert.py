# -*- coding: utf-8 -*-
"""Этап CONVERT: raw/<любой формат> -> raw_md/<...>.md.

Порядок бэкендов при ``auto``:

1. **cardify** — штатный движок СВА, если пакет установлен;
2. **markitdown** — если есть он;
3. **builtin** — конвертеры на мейнстрим-библиотеках (python-docx, python-pptx,
   openpyxl, pdfplumber/pypdf);
4. **stdlib** — csv, json, ipynb, html.parser, email: работают всегда.

Если выбранный бэкенд упал — следующий по списку подхватывает автоматически.
Каждый результат получает YAML-frontmatter с провенансом источника.
"""

from __future__ import annotations

import base64
import csv
import html
import io
import json
import re
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .config import ENCODINGS, MD_DIR_NAME, RAW_DIR_NAME, S, VERSION
from .state import file_fingerprint

ConverterFn = Callable[[Path, Path], Tuple[str, Dict[str, Any], List[str]]]


# =====================================================================================
# Вспомогательные утилиты
# =====================================================================================

def read_text_any_encoding(path: Path) -> Tuple[str, str]:
    """Прочитать текст, перебирая кодировки. Возвращает (текст, кодировка)."""
    data = path.read_bytes()
    for encoding in ENCODINGS:
        try:
            return data.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace"), "utf-8/replace"


def _md_cell(value: Any) -> str:
    text = "" if value is None else str(value)
    return text.replace("|", "\\|").replace("\n", " ").strip()


def _md_table(rows: Sequence[Sequence[Any]], limit: Optional[int] = None) -> str:
    limit = limit or S.max_table_rows
    rows = [r for r in rows if any(_md_cell(cell) for cell in r)]
    if not rows:
        return ""
    truncated = len(rows) > limit
    rows = rows[:limit]
    width = max(len(r) for r in rows)

    lines = ["| " + " | ".join(_md_cell(x) for x in list(rows[0]) + [""] * (width - len(rows[0]))) + " |",
             "| " + " | ".join("---" for _ in range(width)) + " |"]
    for row in rows[1:]:
        padded = list(row) + [""] * (width - len(row))
        lines.append("| " + " | ".join(_md_cell(x) for x in padded) + " |")
    if truncated:
        lines += ["", f"_(таблица усечена до {limit} строк)_"]
    return "\n".join(lines)


def html_to_text(text: str) -> str:
    """Мини-конвертер HTML -> Markdown-ish на stdlib html.parser."""
    from html.parser import HTMLParser

    class Parser(HTMLParser):
        def __init__(self) -> None:
            super().__init__(convert_charrefs=True)
            self.parts: List[str] = []
            self.skip = 0

        def handle_starttag(self, tag: str, attrs: Any) -> None:
            if tag in {"script", "style", "head"}:
                self.skip += 1
            elif tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
                self.parts.append("\n\n" + "#" * int(tag[1]) + " ")
            elif tag in {"p", "div", "section", "article", "tr", "table", "ul", "ol", "pre"}:
                self.parts.append("\n\n")
            elif tag == "li":
                self.parts.append("\n- ")
            elif tag == "br":
                self.parts.append("\n")

        def handle_endtag(self, tag: str) -> None:
            if tag in {"script", "style", "head"} and self.skip:
                self.skip -= 1
            elif tag in {"h1", "h2", "h3", "h4", "h5", "h6", "p", "td", "th"}:
                self.parts.append("\n")

        def handle_data(self, data: str) -> None:
            if not self.skip:
                self.parts.append(data)

    parser = Parser()
    parser.feed(text)
    raw = html.unescape("".join(parser.parts))
    raw = re.sub(r"[ \t]+", " ", raw)
    return re.sub(r"\n{3,}", "\n\n", raw).strip()


def yaml_frontmatter(meta: Dict[str, Any]) -> str:
    """Минимальный YAML-дампер: строки, числа, списки. Без pyyaml."""

    def dump(value: Any) -> str:
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, (int, float)):
            return str(value)
        text = str(value)
        if text == "":
            return '""'
        if re.search(r"[:\n#\"'\[\]{}]|^\s|\s$", text):
            return '"' + text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ") + '"'
        return text

    lines = ["---"]
    for key, value in meta.items():
        if value is None:
            continue
        if isinstance(value, (list, tuple)):
            if not value:
                continue
            lines.append(f"{key}:")
            lines.extend(f"  - {dump(item)}" for item in value)
        else:
            lines.append(f"{key}: {dump(value)}")
    lines.append("---")
    return "\n".join(lines)


# =====================================================================================
# Бэкенд 1: cardify
# =====================================================================================

_cardify_cache: Optional[Tuple[bool, str]] = None
_markitdown_cache: Optional[Tuple[bool, str]] = None


def _hush_cardify() -> None:
    """cardify по умолчанию сыплет INFO-логами в stdout — приглушаем их."""
    try:
        import logging

        from cardify.logging_setup import configure_logging
        from cardify.models.config import LoggingConfig

        configure_logging(
            LoggingConfig(
                log_dir=Path(tempfile.gettempdir()) / "llm_wiki_logs",
                file_level="WARNING",
                stdout_level="INFO" if S.verbose else "ERROR",
                show_warnings=S.verbose,
            )
        )
        if not S.verbose:
            logging.getLogger("cardify").setLevel(logging.ERROR)
    except Exception:
        pass


def cardify_available() -> Tuple[bool, str]:
    global _cardify_cache
    if _cardify_cache is not None:
        return _cardify_cache
    try:
        import cardify
        from cardify.convert import registry as _registry  # регистрирует конвертеры
        assert _registry is not None
    except Exception as exc:
        _cardify_cache = (False, f"{type(exc).__name__}: {exc}")
        return _cardify_cache
    _hush_cardify()
    _cardify_cache = (True, f"cardify {getattr(cardify, '__version__', '?')}")
    return _cardify_cache


def cardify_extensions() -> Tuple[str, ...]:
    try:
        from cardify.convert.registry import list_supported_extensions

        return list_supported_extensions()
    except Exception:
        return ()


def convert_with_cardify(source: Path, assets_dir: Path) -> Tuple[str, Dict[str, Any], List[str]]:
    from cardify.convert.base import ConverterOptions
    from cardify.convert.registry import get_converter

    converter_cls = get_converter(source.suffix)
    tmp_dir = Path(tempfile.mkdtemp(prefix="cardify-"))
    try:
        image_mode = "base64" if S.image_mode == "base64" else "folder"
        result = converter_cls().convert(source, ConverterOptions(output_dir=tmp_dir, image_mode=image_mode))
        markdown = Path(result.markdown_path).read_text(encoding="utf-8")

        meta: Dict[str, Any] = dict(getattr(result, "metadata", {}) or {})
        warnings: List[str] = []
        for item in getattr(result, "warnings", []) or []:
            warnings.append(f"{getattr(item, 'code', 'WARNING')}: {getattr(item, 'message', item)}")
        for item in getattr(result, "skipped_elements", []) or []:
            warnings.append(
                "skipped {0} @ {1}: {2}".format(
                    getattr(item, "element_type", "?"),
                    getattr(item, "location", "?"),
                    getattr(item, "reason", "?"),
                )
            )

        images_src = tmp_dir / "images"
        if S.image_mode == "folder" and images_src.is_dir() and any(images_src.iterdir()):
            if assets_dir.exists():
                shutil.rmtree(assets_dir, ignore_errors=True)
            shutil.copytree(images_src, assets_dir)
            markdown = markdown.replace("](images/", f"]({assets_dir.name}/")
        elif S.image_mode == "skip":
            markdown = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", markdown)

        # cardify пропускает сгруппированные фигуры PPTX — весь текст внутри групп
        # просто теряется. Дочитываем его python-pptx и дописываем отдельной секцией.
        if source.suffix.lower() == ".pptx" and any("complex_shape" in w for w in warnings):
            recovered = _pptx_group_texts(source, markdown)
            if recovered:
                markdown += "\n\n## Текст сгруппированных объектов\n\n" + recovered
                warnings = [w for w in warnings if "complex_shape" not in w]
                warnings.append(
                    "cardify пропустил сгруппированные фигуры — текст восстановлен через python-pptx"
                )

        meta.setdefault("converted_by", f"cardify:{converter_cls.__name__}")
        return markdown, meta, warnings
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _pptx_group_texts(source: Path, existing_markdown: str) -> str:
    """Текст из сгруппированных фигур, которого ещё нет в markdown."""
    try:
        from pptx import Presentation
    except ImportError:
        return ""
    try:
        presentation = Presentation(str(source))
    except Exception:
        return ""

    chunks: List[str] = []
    for number, slide in enumerate(presentation.slides, start=1):
        texts: List[str] = []
        for shape in _iter_pptx_shapes(slide.shapes, groups_only=True):
            if getattr(shape, "has_text_frame", False):
                text = shape.text_frame.text.strip()
                if text and text not in existing_markdown:
                    texts.append(text)
        if texts:
            chunks.append(f"### Слайд {number}\n\n" + "\n\n".join(dict.fromkeys(texts)))
    return "\n\n".join(chunks)


def _iter_pptx_shapes(shapes: Any, groups_only: bool = False) -> Any:
    """Обойти фигуры слайда, рекурсивно раскрывая группы."""
    try:
        from pptx.enum.shapes import MSO_SHAPE_TYPE

        group_type = MSO_SHAPE_TYPE.GROUP
    except Exception:
        group_type = None

    for shape in shapes:
        is_group = group_type is not None and getattr(shape, "shape_type", None) == group_type
        if is_group:
            yield from _iter_pptx_shapes(shape.shapes, groups_only=False)
        elif not groups_only:
            yield shape


# =====================================================================================
# Бэкенд 2: markitdown
# =====================================================================================

def markitdown_available() -> Tuple[bool, str]:
    global _markitdown_cache
    if _markitdown_cache is not None:
        return _markitdown_cache
    try:
        import markitdown
    except Exception as exc:
        _markitdown_cache = (False, f"{type(exc).__name__}: {exc}")
        return _markitdown_cache
    _markitdown_cache = (True, f"markitdown {getattr(markitdown, '__version__', '?')}")
    return _markitdown_cache


def convert_with_markitdown(source: Path) -> Tuple[str, Dict[str, Any], List[str]]:
    """MarkItDown без сетевых интеграций: в конструктор идут только выключающие параметры."""
    import inspect

    import markitdown

    safe = {
        "llm_client": None,
        "docintel_endpoint": None,
        "mlm_client": None,
        "enable_plugins": False,
        "requests_session": None,
    }
    signature = inspect.signature(markitdown.MarkItDown.__init__)
    converter = markitdown.MarkItDown(**{k: v for k, v in safe.items() if k in signature.parameters})

    result = converter.convert_local(str(source))
    markdown = getattr(result, "markdown", None) or getattr(result, "text_content", "")
    if S.image_mode != "base64":
        markdown = re.sub(r"!\[[^\]]*\]\(data:image/[^)]*\)", "", markdown)
    return markdown, {"converted_by": "markitdown"}, []


# =====================================================================================
# Бэкенд 3: встроенные конвертеры
# =====================================================================================

def _builtin_docx(source: Path, assets_dir: Path) -> Tuple[str, Dict[str, Any], List[str]]:
    from docx import Document
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    document = Document(str(source))
    parts: List[str] = []

    body = document.element.body
    for child in body.iterchildren():
        tag = child.tag.split("}")[-1]
        if tag == "p":
            paragraph = Paragraph(child, document)
            text = paragraph.text.strip()
            if not text:
                continue
            style = (paragraph.style.name or "").lower()
            match = re.search(r"(\d+)", style)
            if ("heading" in style or "заголовок" in style) and match:
                parts.append("#" * min(int(match.group(1)), 6) + " " + text)
            elif "title" in style or "название" in style:
                parts.append("# " + text)
            elif "list" in style or "список" in style:
                parts.append("- " + text)
            else:
                parts.append(text)
        elif tag == "tbl":
            rendered = _md_table([[cell.text for cell in row.cells] for row in Table(child, document).rows])
            if rendered:
                parts.append(rendered)

    core = document.core_properties
    meta = {
        k: v
        for k, v in {
            "author": core.author or None,
            "title": core.title or None,
            "subject": core.subject or None,
            "created": core.created.isoformat() if core.created else None,
            "modified": core.modified.isoformat() if core.modified else None,
        }.items()
        if v
    }

    warnings: List[str] = []
    images = _save_docx_images(document, assets_dir)
    if images:
        meta["image_count"] = len(images)
        parts.append("## Изображения\n\n" + "\n\n".join(images))
    if "OLEObject" in body.xml or "<w:object" in body.xml:
        warnings.append("skipped embedded_ole_object: встроенные OLE-объекты не извлекаются")

    meta["converted_by"] = "builtin:python-docx"
    return "\n\n".join(parts), meta, warnings


def _save_docx_images(document: Any, assets_dir: Path) -> List[str]:
    if S.image_mode == "skip":
        return []
    try:
        from docx.opc.constants import RELATIONSHIP_TYPE as REL_TYPE
    except Exception:
        return []

    refs: List[str] = []
    count = 0
    for rel in document.part.rels.values():
        if rel.reltype != REL_TYPE.IMAGE:
            continue
        count += 1
        blob = rel.target_part.blob
        mime = rel.target_part.content_type
        if S.image_mode == "base64":
            refs.append(f"![](data:{mime};base64,{base64.b64encode(blob).decode('ascii')})")
        else:
            ext = "." + mime.rsplit("/", 1)[-1].replace("jpeg", "jpg").replace("x-emf", "emf")
            assets_dir.mkdir(parents=True, exist_ok=True)
            name = f"image_{count}{ext}"
            (assets_dir / name).write_bytes(blob)
            refs.append(f"![]({assets_dir.name}/{name})")
    return refs


def _builtin_pptx(source: Path, assets_dir: Path) -> Tuple[str, Dict[str, Any], List[str]]:
    from pptx import Presentation

    presentation = Presentation(str(source))
    parts: List[str] = []
    image_count = 0
    number = 0
    for number, slide in enumerate(presentation.slides, start=1):
        parts.append(f"## Слайд {number}")
        for shape in _iter_pptx_shapes(slide.shapes):  # группы раскрываются рекурсивно
            if getattr(shape, "has_text_frame", False):
                text = "\n".join(p.text for p in shape.text_frame.paragraphs).strip()
                if text:
                    parts.append(text)
            if getattr(shape, "has_table", False):
                rendered = _md_table([[cell.text for cell in row.cells] for row in shape.table.rows])
                if rendered:
                    parts.append(rendered)
            if S.image_mode == "folder" and getattr(shape, "image", None) is not None:
                try:
                    image = shape.image
                    image_count += 1
                    assets_dir.mkdir(parents=True, exist_ok=True)
                    name = f"slide{number}_img{image_count}.{image.ext}"
                    (assets_dir / name).write_bytes(image.blob)
                    parts.append(f"![]({assets_dir.name}/{name})")
                except Exception:
                    pass
        if slide.has_notes_slide:
            notes = (slide.notes_slide.notes_text_frame.text or "").strip()
            if notes:
                parts.append(f"**Заметки докладчика:** {notes}")

    meta: Dict[str, Any] = {"slide_count": number, "converted_by": "builtin:python-pptx"}
    if image_count:
        meta["image_count"] = image_count
    return "\n\n".join(parts), meta, []


def _builtin_xlsx(source: Path, _assets: Path) -> Tuple[str, Dict[str, Any], List[str]]:
    from openpyxl import load_workbook

    workbook = load_workbook(str(source), read_only=True, data_only=True)
    parts: List[str] = []
    sheets = 0
    for sheet in workbook.worksheets:
        sheets += 1
        parts.append(f"## Лист: {sheet.title}")
        rows: List[List[Any]] = []
        for row in sheet.iter_rows(values_only=True):
            rows.append(["" if v is None else v for v in row])
            if len(rows) > S.max_table_rows + 1:
                break
        parts.append(_md_table(rows) or "_(пустой лист)_")
    workbook.close()
    return "\n\n".join(parts), {"sheet_count": sheets, "converted_by": "builtin:openpyxl"}, []


def _builtin_pdf(source: Path, _assets: Path) -> Tuple[str, Dict[str, Any], List[str]]:
    pages: List[str] = []
    warnings: List[str] = []
    try:
        import pdfplumber

        backend = "builtin:pdfplumber"
        with pdfplumber.open(str(source)) as pdf:
            for number, page in enumerate(pdf.pages, start=1):
                text = (page.extract_text() or "").strip()
                pages.append(f"## Страница {number}\n\n" + (text or "_(нет текстового слоя)_"))
    except ImportError:
        from pypdf import PdfReader

        backend = "builtin:pypdf"
        for number, page in enumerate(PdfReader(str(source)).pages, start=1):
            text = (page.extract_text() or "").strip()
            pages.append(f"## Страница {number}\n\n" + (text or "_(нет текстового слоя)_"))

    body = "\n\n".join(pages)
    if len(re.sub(r"\s", "", body)) < 40 * max(1, len(pages)):
        warnings.append("PDF почти без текстового слоя — вероятно, скан. Нужен OCR.")
    return body, {"page_count": len(pages), "converted_by": backend}, warnings


def _builtin_ipynb(source: Path, _assets: Path) -> Tuple[str, Dict[str, Any], List[str]]:
    data = json.loads(read_text_any_encoding(source)[0])
    language = (
        data.get("metadata", {}).get("kernelspec", {}).get("language")
        or data.get("metadata", {}).get("language_info", {}).get("name")
        or "python"
    )
    parts: List[str] = []
    for cell in data.get("cells", []):
        text = "".join(cell.get("source", [])).rstrip()
        if not text:
            continue
        if cell.get("cell_type") == "markdown":
            parts.append(text)
        elif cell.get("cell_type") == "code":
            parts.append(f"```{language}\n{text}\n```")
            outputs: List[str] = []
            for out in cell.get("outputs", []):
                if "text" in out:
                    outputs.append("".join(out["text"]))
                elif out.get("data", {}).get("text/plain"):
                    outputs.append("".join(out["data"]["text/plain"]))
            joined = "".join(outputs).strip()
            if joined:
                if len(joined) > 2000:
                    joined = joined[:2000] + "\n... (вывод усечён)"
                parts.append(f"_Вывод:_\n\n```\n{joined}\n```")
    return "\n\n".join(parts), {"converted_by": "builtin:ipynb", "language": language}, []


def _builtin_tabular(source: Path, _assets: Path) -> Tuple[str, Dict[str, Any], List[str]]:
    text, encoding = read_text_any_encoding(source)
    delimiter = "\t" if source.suffix.lower() == ".tsv" else None
    if delimiter is None:
        try:
            delimiter = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|").delimiter
        except csv.Error:
            delimiter = ","
    rows = list(csv.reader(io.StringIO(text), delimiter=delimiter))
    meta = {
        "converted_by": "builtin:csv",
        "encoding": encoding,
        "row_count": len(rows),
        "delimiter": delimiter,
    }
    return _md_table(rows), meta, []


def _builtin_html(source: Path, _assets: Path) -> Tuple[str, Dict[str, Any], List[str]]:
    text, encoding = read_text_any_encoding(source)
    return html_to_text(text), {"converted_by": "builtin:html.parser", "encoding": encoding}, []


def _builtin_eml(source: Path, _assets: Path) -> Tuple[str, Dict[str, Any], List[str]]:
    from email import policy
    from email.parser import BytesParser

    with source.open("rb") as handle:
        message = BytesParser(policy=policy.default).parse(handle)

    headers = [
        f"**От:** {message.get('From', '')}",
        f"**Кому:** {message.get('To', '')}",
        f"**Дата:** {message.get('Date', '')}",
        f"**Тема:** {message.get('Subject', '')}",
    ]
    body_part = message.get_body(preferencelist=("plain", "html"))
    body = ""
    if body_part is not None:
        content = body_part.get_content()
        body = html_to_text(content) if body_part.get_content_type() == "text/html" else content

    attachments = [p.get_filename() for p in message.iter_attachments() if p.get_filename()]
    meta = {
        "converted_by": "builtin:email",
        "subject": message.get("Subject", ""),
        "attachments": len(attachments),
    }
    return "\n".join(headers) + "\n\n" + body, meta, [f"вложение не извлечено: {n}" for n in attachments]


def _builtin_code(source: Path, _assets: Path) -> Tuple[str, Dict[str, Any], List[str]]:
    text, encoding = read_text_any_encoding(source)
    lang = {
        ".py": "python", ".sql": "sql", ".sh": "bash", ".xml": "xml",
        ".json": "json", ".jsonl": "json", ".yaml": "yaml", ".yml": "yaml",
    }.get(source.suffix.lower(), "")
    return f"```{lang}\n{text.strip()}\n```", {"converted_by": "builtin:code", "encoding": encoding}, []


def _builtin_text(source: Path, _assets: Path) -> Tuple[str, Dict[str, Any], List[str]]:
    text, encoding = read_text_any_encoding(source)
    return text.strip(), {"converted_by": "builtin:text", "encoding": encoding}, []


BUILTIN_CONVERTERS: Dict[str, ConverterFn] = {
    ".docx": _builtin_docx,
    ".pptx": _builtin_pptx,
    ".xlsx": _builtin_xlsx,
    ".xlsm": _builtin_xlsx,
    ".pdf": _builtin_pdf,
    ".ipynb": _builtin_ipynb,
    ".csv": _builtin_tabular,
    ".tsv": _builtin_tabular,
    ".html": _builtin_html,
    ".htm": _builtin_html,
    ".eml": _builtin_eml,
    ".py": _builtin_code,
    ".sql": _builtin_code,
    ".sh": _builtin_code,
    ".xml": _builtin_code,
    ".json": _builtin_code,
    ".jsonl": _builtin_code,
    ".yaml": _builtin_code,
    ".yml": _builtin_code,
    ".md": _builtin_text,
    ".markdown": _builtin_text,
    ".txt": _builtin_text,
    ".text": _builtin_text,
    ".log": _builtin_text,
    ".ini": _builtin_text,
    ".cfg": _builtin_text,
    ".conf": _builtin_text,
}


# =====================================================================================
# Оркестрация
# =====================================================================================

def md_target(root: Path, rel_source: str) -> Path:
    """raw/a/b.docx -> raw_md/a/b.docx.md ; raw/a/c.md -> raw_md/a/c.md"""
    rel = Path(rel_source)
    parts = rel.parts[1:] if rel.parts and rel.parts[0] == RAW_DIR_NAME else rel.parts
    inner = Path(*parts) if parts else rel
    name = inner.stem + ".md" if inner.suffix.lower() in {".md", ".markdown"} else inner.name + ".md"
    return root / MD_DIR_NAME / inner.parent / name


def choose_backend(extension: str) -> str:
    """Какой бэкенд реально будет использован для расширения."""
    extension = extension.lower()
    if S.convert_backend in {"cardify", "markitdown", "builtin"}:
        return S.convert_backend
    if cardify_available()[0] and extension in cardify_extensions():
        return "cardify"
    if extension in BUILTIN_CONVERTERS:
        return "builtin"
    if markitdown_available()[0]:
        return "markitdown"
    return "builtin"


def convert_source(root: Path, rel_source: str) -> Dict[str, Any]:
    """Сконвертировать один файл raw/* в raw_md/*.md. Возвращает отчёт."""
    source = (root / rel_source).resolve()
    extension = source.suffix.lower()
    target = md_target(root, rel_source)
    assets_dir = target.with_name(target.name[:-3] + ".assets")

    order = [choose_backend(extension)]
    order += [b for b in ("cardify", "builtin", "markitdown") if b not in order]

    errors: List[str] = []
    for backend in order:
        try:
            if backend == "cardify":
                if not cardify_available()[0] or extension not in cardify_extensions():
                    continue
                markdown, meta, warnings = convert_with_cardify(source, assets_dir)
            elif backend == "markitdown":
                if not markitdown_available()[0]:
                    continue
                markdown, meta, warnings = convert_with_markitdown(source)
            else:
                handler = BUILTIN_CONVERTERS.get(extension)
                if handler is None:
                    continue
                markdown, meta, warnings = handler(source, assets_dir)
        except Exception as exc:
            errors.append(f"{backend}: {type(exc).__name__}: {exc}")
            continue

        if not (markdown or "").strip():
            errors.append(f"{backend}: пустой результат конвертации")
            continue

        fingerprint = file_fingerprint(source)
        header: Dict[str, Any] = {
            "source": rel_source,
            "source_sha256": fingerprint["sha256"][:16],
            "source_bytes": fingerprint["size"],
            "format": extension.lstrip("."),
            "converted_at": datetime.now().isoformat(timespec="seconds"),
            "converted_by": meta.pop("converted_by", backend),
            "generator": f"llm_wiki {VERSION}",
        }
        for key, value in meta.items():
            header.setdefault(key, value)
        if warnings:
            header["conversion_warnings"] = warnings

        body = re.sub(r"\n{3,}", "\n\n", markdown.strip())
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(yaml_frontmatter(header) + "\n\n" + body + "\n", encoding="utf-8")

        return {
            "ok": True,
            "source": rel_source,
            "markdown": target.relative_to(root).as_posix(),
            "backend": header["converted_by"],
            "chars": len(body),
            "warnings": warnings,
            "fallbacks": errors,
        }

    return {"ok": False, "source": rel_source, "errors": errors or ["нет подходящего конвертера"]}


def ensure_markdown(root: Path, rel_source: str, state: Dict[str, Any], force: bool = False) -> Dict[str, Any]:
    """Гарантировать свежую markdown-версию источника. Кэш — по sha256 оригинала."""
    fingerprint = file_fingerprint(root / rel_source)
    target = md_target(root, rel_source)
    cached = state.setdefault("converted", {}).get(rel_source)

    if not force and isinstance(cached, dict) and cached.get("sha256") == fingerprint["sha256"] and target.is_file():
        return {
            "ok": True,
            "source": rel_source,
            "markdown": target.relative_to(root).as_posix(),
            "backend": cached.get("backend", "?"),
            "cached": True,
            "warnings": cached.get("warnings", []),
        }

    report = convert_source(root, rel_source)
    if report.get("ok"):
        state["converted"][rel_source] = {
            "sha256": fingerprint["sha256"],
            "markdown": report["markdown"],
            "backend": report["backend"],
            "warnings": report.get("warnings", []),
            "converted_at": datetime.now().isoformat(timespec="seconds"),
        }
    report["cached"] = False
    return report
