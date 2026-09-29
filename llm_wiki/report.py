# -*- coding: utf-8 -*-
"""REPORT: метрики базы знаний и HTML-отчёт.

Считает объём и связность wiki/, сравнивает её с сырым слоем (raw/ по файлам
и размеру, raw_md/ по символам — именно он реально уходит в модель) и рисует
самодостаточный HTML без внешних ресурсов.
"""

from __future__ import annotations

import html
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Set, Tuple

from .config import IGNORED_NAMES, MD_DIR_NAME, RAW_DIR_NAME, VERSION, WIKI_DIR_NAME
from .ui import green, render_footprint

WIKI_LINK_RE = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]")
MD_LINK_RE = re.compile(r"\[([^\]]*)\]\(([^)]+)\)")
CODE_FENCE_RE = re.compile(r"^```", re.MULTILINE)
TABLE_ROW_RE = re.compile(r"^\|.*\|$", re.MULTILINE)

RAW_TEXT_EXTENSIONS = {
    ".md", ".markdown", ".txt", ".json", ".jsonl", ".csv", ".tsv",
    ".yaml", ".yml", ".html", ".xml", ".py", ".sql", ".sh",
}


# =====================================================================================
# Сбор данных
# =====================================================================================

def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        return ""


def collect_files(wiki_root: Path) -> List[Path]:
    return sorted(
        p for p in wiki_root.rglob("*.md")
        if not any(part in IGNORED_NAMES for part in p.parts)
    )


def extract_links(content: str) -> List[Tuple[str, str, str]]:
    """Ссылки страницы: (тип, текст, цель)."""
    links: List[Tuple[str, str, str]] = []
    for m in WIKI_LINK_RE.finditer(content):
        target = m.group(1).strip()
        links.append(("wiki", target, target))
    for m in MD_LINK_RE.finditer(content):
        target = m.group(2).strip()
        kind = "external" if target.startswith(("http://", "https://", "mailto:", "#")) else "md"
        links.append((kind, m.group(1), target))
    return links


def resolve_link(link_type: str, target: str, current_file: Path, wiki_root: Path, all_files: Set[Path]) -> bool:
    if link_type == "external":
        return True
    target = target.split("#")[0].strip()
    if not target:
        return True

    if link_type == "wiki":
        candidate = wiki_root / target
        if not candidate.suffix:
            candidate = candidate.with_suffix(".md")
        if candidate in all_files:
            return True
        name = Path(target).name
        if not name.endswith(".md"):
            name += ".md"
        return any(f.name == name for f in all_files)

    candidate = (current_file.parent / target).resolve()
    if candidate in all_files:
        return True
    if not candidate.suffix and candidate.with_suffix(".md") in all_files:
        return True
    return False


def analyze_source_layer(project_root: Path) -> Dict[str, Any]:
    """Статистика сырого слоя: raw/ по файлам, raw_md/ по тексту."""
    raw_root = project_root / RAW_DIR_NAME
    md_root = project_root / MD_DIR_NAME
    stats: Dict[str, Any] = {
        "exists": raw_root.is_dir(),
        "total_files": 0,
        "total_size_mb": 0.0,
        "extensions": Counter(),
        "converted_files": 0,
        "total_chars": 0,
        "total_words": 0,
    }
    if not stats["exists"]:
        return stats

    total_size = 0
    for item in raw_root.rglob("*"):
        if item.is_file() and not item.name.startswith("."):
            stats["total_files"] += 1
            total_size += item.stat().st_size
            stats["extensions"][item.suffix.lower() or "(без расширения)"] += 1
    stats["total_size_mb"] = total_size / (1024 * 1024)

    # Символы считаем по markdown-зеркалу: именно этот текст видит модель.
    if md_root.is_dir():
        for item in md_root.rglob("*.md"):
            content = _read(item)
            if content:
                stats["converted_files"] += 1
                stats["total_chars"] += len(content)
                stats["total_words"] += len(content.split())
    else:  # зеркала нет — считаем текстовые файлы прямо из raw/
        for item in raw_root.rglob("*"):
            if item.is_file() and item.suffix.lower() in RAW_TEXT_EXTENSIONS:
                content = _read(item)
                stats["total_chars"] += len(content)
                stats["total_words"] += len(content.split())
    return stats


def analyze_wiki(wiki_root: Path) -> Dict[str, Any]:
    files = collect_files(wiki_root)
    all_files = set(files)

    stats: Dict[str, Any] = {
        "wiki_root": str(wiki_root),
        "project_root": str(wiki_root.parent),
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "total_files": len(files),
        "total_chars": 0, "total_lines": 0, "total_words": 0,
        "total_code_blocks": 0, "total_tables": 0,
        "total_links": 0, "internal_links": 0, "external_links": 0,
        "broken_links": 0, "orphan_pages": 0,
        "files_per_dir": Counter(),
        "file_details": [], "broken_link_details": [], "orphan_details": [],
        "link_graph": defaultdict(list), "incoming_links": defaultdict(list),
        "size_distribution": [],
    }

    contents: Dict[Path, str] = {}
    for f in files:
        content = _read(f)
        contents[f] = content
        rel = f.relative_to(wiki_root).as_posix()
        chars = len(content)

        stats["total_chars"] += chars
        stats["total_lines"] += content.count("\n") + (1 if content and not content.endswith("\n") else 0)
        stats["total_words"] += len(content.split())
        stats["total_code_blocks"] += len(CODE_FENCE_RE.findall(content)) // 2
        stats["total_tables"] += len(TABLE_ROW_RE.findall(content))
        stats["files_per_dir"][f.parent.relative_to(wiki_root).as_posix()] += 1
        stats["size_distribution"].append(chars)
        stats["file_details"].append({
            "path": rel,
            "chars": chars,
            "lines": content.count("\n") + 1,
            "words": len(content.split()),
            "code_blocks": len(CODE_FENCE_RE.findall(content)) // 2,
        })

    for f in files:
        rel = f.relative_to(wiki_root).as_posix()
        for link_type, _text, target in extract_links(contents[f]):
            stats["total_links"] += 1
            if link_type == "external":
                stats["external_links"] += 1
                continue

            stats["internal_links"] += 1
            if not resolve_link(link_type, target, f, wiki_root, all_files):
                stats["broken_links"] += 1
                stats["broken_link_details"].append({"source": rel, "target": target, "type": link_type})
                continue

            clean = target.split("#")[0].strip()
            resolved: Path | None = None
            if link_type == "wiki":
                name = Path(clean).name
                if not name.endswith(".md"):
                    name += ".md"
                resolved = next((tf for tf in all_files if tf.name == name), None)
            else:
                candidate = (f.parent / clean).resolve()
                if not candidate.suffix:
                    candidate = candidate.with_suffix(".md")
                resolved = candidate if candidate in all_files else None
            if resolved is not None:
                target_rel = resolved.relative_to(wiki_root).as_posix()
                stats["link_graph"][rel].append(target_rel)
                stats["incoming_links"][target_rel].append(rel)

    for f in files:
        rel = f.relative_to(wiki_root).as_posix()
        if rel == "index.md":
            continue
        if not stats["incoming_links"].get(rel):
            stats["orphan_pages"] += 1
            stats["orphan_details"].append(rel)

    stats["file_details"].sort(key=lambda x: x["chars"], reverse=True)
    stats["top_files"] = stats["file_details"][:15]
    stats["raw_stats"] = analyze_source_layer(wiki_root.parent)
    return stats


# =====================================================================================
# HTML
# =====================================================================================

def generate_html(stats: Dict[str, Any]) -> str:
    avg_chars = stats["total_chars"] // stats["total_files"] if stats["total_files"] else 0
    avg_words = stats["total_words"] // stats["total_files"] if stats["total_files"] else 0
    orphan_pct = (stats["orphan_pages"] / stats["total_files"] * 100) if stats["total_files"] else 0
    broken_pct = (stats["broken_links"] / stats["total_links"] * 100) if stats["total_links"] else 0
    footprint = render_footprint()

    sizes = stats["size_distribution"]
    bins: List[int] = []
    bin_labels: List[str] = []
    if sizes:
        min_s, max_s = min(sizes), max(sizes)
        step = (max_s - min_s) / 5 if max_s > min_s else 1
        bins = [0] * 5
        for s in sizes:
            bins[min(int((s - min_s) / step), 4)] += 1
        bin_labels = [f"{int(min_s + i * step) // 1000}k–{int(min_s + (i + 1) * step) // 1000}k" for i in range(5)]
    max_bin = max(bins) if bins else 1

    top_files_rows = "".join(
        f"<tr><td><code>{html.escape(f['path'])}</code></td>"
        f"<td class='num'>{f['chars']:,}</td><td class='num'>{f['lines']:,}</td>"
        f"<td class='num'>{f['words']:,}</td><td class='num'>{f['code_blocks']}</td></tr>"
        for f in stats["top_files"]
    )

    orphan_items = "".join(f"<li><code>{html.escape(p)}</code></li>" for p in stats["orphan_details"][:30])
    if len(stats["orphan_details"]) > 30:
        orphan_items += f'<li class="muted">… и ещё {len(stats["orphan_details"]) - 30}</li>'

    broken_items = "".join(
        f"<tr><td><code>{html.escape(b['source'])}</code></td>"
        f"<td><code>{html.escape(b['target'])}</code></td><td>{b['type']}</td></tr>"
        for b in stats["broken_link_details"][:30]
    )
    if len(stats["broken_link_details"]) > 30:
        broken_items += f'<tr><td colspan="3" class="muted">… и ещё {len(stats["broken_link_details"]) - 30}</td></tr>'

    def bars(pairs: List[Tuple[str, int]]) -> str:
        if not pairs:
            return '<p class="muted">нет данных</p>'
        top = max(pairs[0][1], 1)
        return "".join(
            f'<div class="bar-row"><span class="bar-label">{html.escape(str(name))}</span>'
            f'<div class="bar-track"><div class="bar-fill" style="width:{count / top * 100:.0f}%"></div></div>'
            f'<span class="bar-value">{count}</span></div>'
            for name, count in pairs
        )

    dir_bars = bars(stats["files_per_dir"].most_common(15))
    hist_bars = "".join(
        f'<div class="hist-col"><div class="hist-bar" style="height:{count / max_bin * 100:.0f}%">'
        f'<span class="hist-val">{count}</span></div><div class="hist-label">{label}</div></div>'
        for count, label in zip(bins, bin_labels)
    )

    most_linked = sorted(stats["link_graph"].items(), key=lambda x: len(x[1]), reverse=True)[:10]
    most_linked_to = sorted(stats["incoming_links"].items(), key=lambda x: len(x[1]), reverse=True)[:10]
    linked_rows = "".join(
        f"<tr><td><code>{html.escape(p)}</code></td><td class='num'>{len(t)}</td></tr>" for p, t in most_linked
    ) or '<tr><td class="muted">нет данных</td><td></td></tr>'
    linked_to_rows = "".join(
        f"<tr><td><code>{html.escape(p)}</code></td><td class='num'>{len(s)}</td></tr>" for p, s in most_linked_to
    ) or '<tr><td class="muted">нет данных</td><td></td></tr>'

    raw = stats["raw_stats"]
    if raw["exists"]:
        ratio = "—"
        if raw["total_chars"] and stats["total_chars"]:
            ratio = f"{raw['total_chars'] / stats['total_chars']:.1f}x"
        raw_html = f"""
        <h2>Сырой слой ({RAW_DIR_NAME}/ → {MD_DIR_NAME}/)</h2>
        <div class="metrics-grid">
          <div class="metric-card accent3"><div class="metric-value">{raw['total_files']}</div>
            <div class="metric-label">Файлов в {RAW_DIR_NAME}/</div></div>
          <div class="metric-card accent3"><div class="metric-value">{raw['total_size_mb']:.1f} MB</div>
            <div class="metric-label">Объём {RAW_DIR_NAME}/</div></div>
          <div class="metric-card accent3"><div class="metric-value">{raw['converted_files']}</div>
            <div class="metric-label">Сконвертировано в {MD_DIR_NAME}/</div></div>
          <div class="metric-card accent3"><div class="metric-value">{raw['total_chars']:,}</div>
            <div class="metric-label">Символов текста</div></div>
          <div class="metric-card accent"><div class="metric-value">{ratio}</div>
            <div class="metric-label">Сжатие в wiki</div></div>
        </div>
        <div class="card" style="margin-top:20px;">
          <h3>Форматы источников</h3>
          {bars(raw['extensions'].most_common(15))}
        </div>"""
    else:
        raw_html = f'<p class="muted">Каталог {RAW_DIR_NAME}/ не найден</p>'

    orphan_block = f"""
<h2>Страницы-сироты ({stats['orphan_pages']})</h2>
<div class="card"><ul class="plain">{orphan_items}</ul></div>""" if stats["orphan_details"] else ""

    broken_block = f"""
<h2>Битые ссылки ({stats['broken_links']})</h2>
<div class="card" style="overflow-x:auto;">
  <table><thead><tr><th>Источник</th><th>Цель</th><th>Тип</th></tr></thead>
  <tbody>{broken_items}</tbody></table>
</div>""" if stats["broken_link_details"] else ""

    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>LLM-wiki — отчёт по базе знаний</title>
<!-- generated by llm_wiki {VERSION} :: {html.escape(footprint)} -->
<style>
  :root {{
    --bg:#0f1117; --card:#1a1d27; --card-hover:#222636; --border:#2a2e3e;
    --text:#e4e6eb; --muted:#8b8fa3; --accent:#7c5cfc; --accent2:#00d4aa;
    --accent3:#ff6b9d; --warn:#f39c12; --danger:#e74c3c; --ok:#27ae60;
  }}
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{ background:var(--bg); color:var(--text); font-family:'Segoe UI',system-ui,-apple-system,sans-serif;
         line-height:1.6; padding:24px; max-width:1200px; margin:0 auto; }}
  h1 {{ font-size:1.8rem; background:linear-gradient(135deg,var(--accent),var(--accent2));
        -webkit-background-clip:text; -webkit-text-fill-color:transparent; background-clip:text; margin-bottom:4px; }}
  h2 {{ font-size:1.15rem; margin:32px 0 16px; padding-bottom:8px; border-bottom:1px solid var(--border); }}
  h3 {{ font-size:0.95rem; margin-bottom:12px; }}
  .subtitle {{ color:var(--muted); font-size:0.9rem; margin-bottom:28px; }}
  .metrics-grid {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(170px,1fr)); gap:14px; }}
  .metric-card {{ background:var(--card); border:1px solid var(--border); border-radius:12px; padding:18px 16px;
                  transition:transform .15s,border-color .15s; }}
  .metric-card:hover {{ transform:translateY(-2px); border-color:var(--accent); }}
  .metric-value {{ font-size:1.9rem; font-weight:700; line-height:1.1; }}
  .metric-label {{ color:var(--muted); font-size:0.8rem; margin-top:4px; text-transform:uppercase; letter-spacing:.5px; }}
  .metric-card.accent .metric-value {{ color:var(--accent); }}
  .metric-card.accent2 .metric-value {{ color:var(--accent2); }}
  .metric-card.accent3 .metric-value {{ color:var(--accent3); }}
  .metric-card.warn .metric-value {{ color:var(--warn); }}
  .metric-card.danger .metric-value {{ color:var(--danger); }}
  .two-col {{ display:grid; grid-template-columns:1fr 1fr; gap:20px; }}
  @media (max-width:768px) {{ .two-col {{ grid-template-columns:1fr; }} }}
  .card {{ background:var(--card); border:1px solid var(--border); border-radius:12px; padding:20px; }}
  table {{ width:100%; border-collapse:collapse; font-size:0.85rem; }}
  th {{ text-align:left; color:var(--muted); font-weight:600; padding:8px 10px; border-bottom:1px solid var(--border);
        text-transform:uppercase; font-size:0.72rem; letter-spacing:.5px; }}
  td {{ padding:7px 10px; border-bottom:1px solid var(--border); }}
  tr:hover td {{ background:var(--card-hover); }}
  td.num {{ text-align:right; font-variant-numeric:tabular-nums; color:var(--accent2); }}
  code {{ background:#12141c; padding:2px 6px; border-radius:4px; font-size:0.82rem; color:var(--accent3); word-break:break-all; }}
  .bar-row {{ display:flex; align-items:center; gap:10px; margin-bottom:8px; }}
  .bar-label {{ width:220px; font-size:0.82rem; color:var(--muted); text-align:right; overflow:hidden;
                text-overflow:ellipsis; white-space:nowrap; }}
  .bar-track {{ flex:1; background:#12141c; border-radius:6px; height:22px; overflow:hidden; }}
  .bar-fill {{ height:100%; background:linear-gradient(90deg,var(--accent),var(--accent2)); border-radius:6px; }}
  .bar-value {{ width:40px; font-size:0.82rem; font-weight:600; color:var(--accent2); }}
  .histogram {{ display:flex; align-items:flex-end; gap:12px; height:160px; padding:16px 8px 0; }}
  .hist-col {{ flex:1; display:flex; flex-direction:column; align-items:center; justify-content:flex-end; height:100%; }}
  .hist-bar {{ width:100%; background:linear-gradient(180deg,var(--accent3),var(--accent)); border-radius:6px 6px 0 0;
               min-height:4px; display:flex; align-items:flex-start; justify-content:center; padding-top:4px; }}
  .hist-val {{ font-size:0.72rem; font-weight:700; color:#fff; }}
  .hist-label {{ font-size:0.7rem; color:var(--muted); margin-top:6px; }}
  .muted {{ color:var(--muted); }}
  .health-badge {{ display:inline-block; padding:3px 10px; border-radius:20px; font-size:0.78rem; font-weight:600; }}
  .health-ok {{ background:rgba(39,174,96,.15); color:var(--ok); }}
  .health-warn {{ background:rgba(243,156,18,.15); color:var(--warn); }}
  .health-danger {{ background:rgba(231,76,60,.15); color:var(--danger); }}
  .health-bar {{ height:8px; background:#12141c; border-radius:4px; overflow:hidden; margin-top:6px; }}
  .health-bar-fill {{ height:100%; border-radius:4px; }}
  ul.plain {{ list-style:none; }}
  ul.plain li {{ padding:4px 0; font-size:0.85rem; }}
  .footer {{ text-align:center; color:var(--muted); font-size:0.8rem; margin-top:40px; padding-top:20px;
             border-top:1px solid var(--border); }}
</style>
</head>
<body>

<h1>LLM-wiki — отчёт по базе знаний</h1>
<p class="subtitle"><code>{html.escape(stats['wiki_root'])}</code><br>Сгенерировано: {stats['generated_at']}</p>

{raw_html}

<h2>Общая статистика wiki</h2>
<div class="metrics-grid">
  <div class="metric-card accent"><div class="metric-value">{stats['total_files']}</div><div class="metric-label">Файлов .md</div></div>
  <div class="metric-card accent2"><div class="metric-value">{stats['total_chars']:,}</div><div class="metric-label">Символов</div></div>
  <div class="metric-card accent2"><div class="metric-value">{stats['total_words']:,}</div><div class="metric-label">Слов</div></div>
  <div class="metric-card"><div class="metric-value">{stats['total_lines']:,}</div><div class="metric-label">Строк</div></div>
  <div class="metric-card"><div class="metric-value">{avg_chars:,}</div><div class="metric-label">Символов / файл</div></div>
  <div class="metric-card"><div class="metric-value">{avg_words:,}</div><div class="metric-label">Слов / файл</div></div>
  <div class="metric-card"><div class="metric-value">{stats['total_code_blocks']:,}</div><div class="metric-label">Блоков кода</div></div>
  <div class="metric-card"><div class="metric-value">{stats['total_tables']:,}</div><div class="metric-label">Строк таблиц</div></div>
</div>

<h2>Связи и навигация</h2>
<div class="metrics-grid">
  <div class="metric-card accent"><div class="metric-value">{stats['total_links']:,}</div><div class="metric-label">Всего ссылок</div></div>
  <div class="metric-card accent2"><div class="metric-value">{stats['internal_links']:,}</div><div class="metric-label">Внутренних</div></div>
  <div class="metric-card"><div class="metric-value">{stats['external_links']:,}</div><div class="metric-label">Внешних</div></div>
  <div class="metric-card danger"><div class="metric-value">{stats['broken_links']:,}</div><div class="metric-label">Битых ссылок</div></div>
  <div class="metric-card warn"><div class="metric-value">{stats['orphan_pages']:,}</div><div class="metric-label">Страниц-сирот</div></div>
</div>

<h2>Здоровье базы</h2>
<div class="two-col">
  <div class="card">
    <h3>Сироты (нет входящих ссылок)</h3>
    <span class="health-badge {'health-ok' if orphan_pct <= 10 else 'health-warn' if orphan_pct <= 20 else 'health-danger'}">
      {orphan_pct:.1f}% страниц — сироты</span>
    <div class="health-bar"><div class="health-bar-fill" style="width:{orphan_pct:.0f}%;
      background:{'#27ae60' if orphan_pct <= 10 else '#f39c12' if orphan_pct <= 20 else '#e74c3c'};"></div></div>
    <p class="muted" style="font-size:.8rem; margin-top:8px;">
      {stats['orphan_pages']} из {stats['total_files']} страниц не имеют входящих ссылок (index.md исключён)</p>
  </div>
  <div class="card">
    <h3>Битые ссылки</h3>
    <span class="health-badge {'health-ok' if broken_pct <= 3 else 'health-warn' if broken_pct <= 10 else 'health-danger'}">
      {broken_pct:.1f}% ссылок — битые</span>
    <div class="health-bar"><div class="health-bar-fill" style="width:{broken_pct:.0f}%;
      background:{'#27ae60' if broken_pct <= 3 else '#f39c12' if broken_pct <= 10 else '#e74c3c'};"></div></div>
    <p class="muted" style="font-size:.8rem; margin-top:8px;">
      {stats['broken_links']} битых из {stats['total_links']} всего</p>
  </div>
</div>

<h2>Файлы по каталогам wiki</h2>
<div class="card">{dir_bars}</div>

<h2>Распределение размеров страниц</h2>
<div class="card"><div class="histogram">{hist_bars}</div></div>

<h2>Топ-15 страниц по размеру</h2>
<div class="card" style="overflow-x:auto;">
  <table>
    <thead><tr><th>Файл</th><th style="text-align:right;">Символов</th><th style="text-align:right;">Строк</th>
    <th style="text-align:right;">Слов</th><th style="text-align:right;">Код</th></tr></thead>
    <tbody>{top_files_rows}</tbody>
  </table>
</div>

<h2>Связанность страниц</h2>
<div class="two-col">
  <div class="card"><h3>Больше всего исходящих ссылок</h3>
    <table><thead><tr><th>Файл</th><th style="text-align:right;">→</th></tr></thead>
    <tbody>{linked_rows}</tbody></table></div>
  <div class="card"><h3>Больше всего входящих ссылок</h3>
    <table><thead><tr><th>Файл</th><th style="text-align:right;">←</th></tr></thead>
    <tbody>{linked_to_rows}</tbody></table></div>
</div>
{orphan_block}
{broken_block}

<div class="footer">
  LLM-wiki v{VERSION} · {stats['generated_at']}<br>
  <span class="muted">{stats['total_files']} страниц · {stats['total_chars']:,} символов · {stats['total_links']:,} ссылок</span><br>
  <span class="muted">{html.escape(footprint)}</span>
</div>

</body>
</html>"""


def do_report(root: Path, output: str = "") -> int:
    """Собрать метрики и записать HTML-отчёт."""
    wiki_root = root / WIKI_DIR_NAME
    if not wiki_root.is_dir():
        print(f"Каталог {WIKI_DIR_NAME}/ не найден в {root}")
        return 2

    print(f"Анализирую {wiki_root} ...", flush=True)
    stats = analyze_wiki(wiki_root)

    target = Path(output).resolve() if output else (root / "wiki_report.html").resolve()
    target.write_text(generate_html(stats), encoding="utf-8")

    raw = stats["raw_stats"]
    print(green(f"\nОтчёт сохранён: {target}"))
    print(f"  Страниц wiki : {stats['total_files']} ({stats['total_chars']:,} симв.)")
    if raw["exists"]:
        print(f"  Источников   : {raw['total_files']} ({raw['total_size_mb']:.1f} MB), "
              f"сконвертировано {raw['converted_files']}")
    print(f"  Ссылок       : {stats['total_links']:,} (битых: {stats['broken_links']})")
    print(f"  Сирот        : {stats['orphan_pages']}")
    return 0
