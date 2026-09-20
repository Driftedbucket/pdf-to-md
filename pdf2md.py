from __future__ import annotations
 
import argparse
import glob
import json
import logging
import os
import re
import sys
import time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
 
try:
    import pdfplumber
except ImportError:  # pragma: no cover
    sys.exit("pdfplumber is required:  pip install pdfplumber")
 
log = logging.getLogger("pdf2md")
 

# **************************************************Tunables**************************************************


PARA_GAP = 0.6   # vertical gap (in line-heights) that starts a new paragraph
MARGIN = 0.08    # top/bottom fraction of the page searched for headers/footers
 
BULLET_RE = re.compile(r"^\s*(?:[•●▪■◦‣∙·*\-–—]|\(cid:\d+\))\s+(?=\S)")
NUMBERED_RE = re.compile(r"^\s*(\d{1,3})[.)]\s+(?=\S)")
LETTERED_RE = re.compile(r"^\s*[a-z]\)\s+(?=\S)")
BOLD_HINTS = ("bold", "black", "heavy", "demi")



# ******************************************Data structures*************************************************


@dataclass(frozen=True)
class Options:
    page_breaks: bool = False           # emit <!-- page N --> markers
    front_matter: bool = False          # prepend YAML front matter
    keep_headers_footers: bool = False  # don't strip repeated header/footer text
    tables: bool = True                 # detect ruled tables
    password: str | None = None
 
 
@dataclass
class Line:
    text: str
    top: float
    bottom: float
    x0: float
    size: float
    bold: bool
 
 
@dataclass
class Table:
    top: float
    rows: list[list[str]]
 
 
@dataclass
class PageData:
    height: float
    lines: list[Line]
    tables: list[Table]
 
 
@dataclass
class Block:
    kind: str  # h1..h4 | para | list | table | comment
    text: str
 
 
class NoTextError(Exception):
    """The PDF has no extractable text layer (probably a scan)."""



#********************************************Text helpers****************************************************


def _escape(text: str) -> str:
    """Escape characters that would otherwise be read as Markdown syntax."""
    text = re.sub(r"([\\`*<])", r"\\\1", text)
    text = re.sub(r"(?<![A-Za-z0-9])_|_(?![A-Za-z0-9])", r"\\_", text)  # keep snake_case
    return re.sub(r"^(#{1,6})(?=\s)", r"\\\1", text)
 
 
def _join(parts: list[str]) -> str:
    """Join wrapped lines, repairing 'hyphen-\\nation'."""
    out = parts[0]
    for nxt in parts[1:]:
        if len(out) > 1 and out.endswith("-") and out[-2].isalpha() and nxt[:1].islower():
            out = out[:-1] + nxt
        else:
            out += " " + nxt
    return out
 
 
def _norm(text: str) -> str:
    return re.sub(r"\d+", "#", text.strip().lower())
 
 
def _in_margin(line: Line, page_height: float) -> bool:
    return line.top < MARGIN * page_height or line.bottom > (1 - MARGIN) * page_height
 
 
def _is_bold(fontname: str) -> bool:
    name = (fontname or "").lower()
    return any(h in name for h in BOLD_HINTS)
 
 
def _line_style(chars: list[dict]) -> tuple[float, bool]:
    chars = [c for c in chars if c["text"].strip()]
    if not chars:
        return 0.0, False
    size = Counter(round(c["size"] * 2) / 2 for c in chars).most_common(1)[0][0]
    bold = sum(_is_bold(c.get("fontname", "")) for c in chars) / len(chars) >= 0.8
    return size, bold



# ****************************************************Tables********************************************************


def _clean_cell(cell) -> str:
    if cell is None:
        return ""
    text = re.sub(r"\s*\n\s*", " ", str(cell)).strip()
    return _escape(text).replace("|", "\\|")
 
 
def _clean_table(raw) -> list[list[str]]:
    rows = [[_clean_cell(c) for c in row] for row in (raw or [])]
    rows = [r for r in rows if any(r)]
    if not rows:
        return []
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    keep = [i for i in range(width) if any(r[i] for r in rows)]
    return [[r[i] for i in keep] for r in rows]
 
 
def _table_to_md(rows: list[list[str]]) -> str:
    if len(rows) == 1 and len(rows[0]) == 1:  # a boxed call-out, not a table
        return "> " + rows[0][0]
    header, *body = rows
    lines = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join("---" for _ in header) + " |",
    ]
    lines += ["| " + " | ".join(r) + " |" for r in body]
    return "\n".join(lines)



# ********************************************Page extraction******************************************************


def _outside(bboxes):
    """Predicate for page.filter(): keep objects whose centre is outside every bbox."""
    def keep(obj) -> bool:
        try:
            cx = (obj["x0"] + obj["x1"]) / 2
            cy = (obj["top"] + obj["bottom"]) / 2
        except KeyError:
            return True
        return not any(b[0] <= cx <= b[2] and b[1] <= cy <= b[3] for b in bboxes)
    return keep
 
 
def _extract_page(page, want_tables: bool) -> PageData:
    tables: list[Table] = []
    bboxes: list[tuple] = []
    if want_tables:
        try:
            for t in page.find_tables():
                bboxes.append(t.bbox)
                rows = _clean_table(t.extract())
                if rows:
                    tables.append(Table(top=t.bbox[1], rows=rows))
        except Exception as exc:  # table detection is best-effort
            log.debug("table detection failed on page %s: %s", page.page_number, exc)
            bboxes, tables = [], []
 
    body = page.filter(_outside(bboxes)) if bboxes else page
    lines: list[Line] = []
    for raw in body.extract_text_lines(return_chars=True):
        text = raw["text"].strip()
        if text:
            size, bold = _line_style(raw["chars"])
            lines.append(Line(text, raw["top"], raw["bottom"], raw["x0"], size, bold))
    return PageData(float(page.height), lines, tables)



# ********************************************Document-level analysis************************************************


def _body_size(pages: list[PageData]) -> float:
    counts: Counter = Counter()
    for p in pages:
        for ln in p.lines:
            counts[ln.size] += len(ln.text)
    return counts.most_common(1)[0][0] if counts else 0.0
 
 
def _repeated_margin_text(pages: list[PageData]) -> set[str]:
    """Text (digits normalised) that recurs in the top/bottom margin on many pages."""
    if len(pages) < 2:
        return set()
    counts: Counter = Counter()
    for p in pages:
        counts.update({_norm(ln.text) for ln in p.lines if _in_margin(ln, p.height)})
    threshold = max(2, len(pages) * 0.5)
    return {t for t, n in counts.items() if t and n >= threshold}
 
 
def _heading_level(line: Line, body: float) -> int:
    if not body or not line.size or len(line.text) > 150:
        return 0
    ratio = line.size / body
    if ratio >= 1.8:
        return 1
    if ratio >= 1.45:
        return 2
    if ratio >= 1.2:
        return 3
    short = len(line.text) <= 80 and not line.text.rstrip().endswith((".", ",", ";", ":"))
    if line.bold and ratio >= 0.95 and short and not _list_item(line.text):
        return 4
    return 0
 
 
def _list_item(text: str) -> str | None:
    if m := BULLET_RE.match(text):
        return "- " + _escape(text[m.end():].strip())
    if m := NUMBERED_RE.match(text):
        return f"{m.group(1)}. " + _escape(text[m.end():].strip())
    if LETTERED_RE.match(text):
        return "- " + _escape(text.strip())
    return None



# *********************************************Block building******************************************************


class _Builder:
    """Accumulates consecutive lines into a single heading / paragraph / list item."""
 
    def __init__(self) -> None:
        self.done: list[tuple[float, Block]] = []
        self.kind: str | None = None
        self.parts: list[str] = []
        self.top = 0.0
        self.bottom = 0.0
 
    def start(self, kind: str, text: str, ln: Line) -> None:
        self.flush()
        self.kind, self.parts, self.top, self.bottom = kind, [text], ln.top, ln.bottom
 
    def extend(self, text: str, ln: Line) -> None:
        self.parts.append(text)
        self.bottom = ln.bottom
 
    def flush(self) -> None:
        if self.kind:
            self.done.append((self.top, Block(self.kind, _join(self.parts))))
        self.kind, self.parts = None, []
 
 
def _page_blocks(page: PageData, body: float, skip: set[str]) -> list[Block]:
    lines = [
        ln for ln in page.lines
        if not (skip and _in_margin(ln, page.height) and _norm(ln.text) in skip)
    ]
    table_tops = [t.top for t in page.tables]
    b = _Builder()
 
    for ln in lines:
        gap = ln.top - b.bottom
        height = max(ln.bottom - ln.top, 1.0)
        split = (
            b.kind is None
            or gap > PARA_GAP * height
            or any(b.bottom <= t <= ln.top for t in table_tops)
        )
 
        if level := _heading_level(ln, body):
            kind = f"h{level}"
            if b.kind == kind and not split:
                b.extend(_escape(ln.text), ln)      # multi-line heading
            else:
                b.start(kind, _escape(ln.text), ln)
        elif (item := _list_item(ln.text)) is not None:
            b.start("list", item, ln)
        elif b.kind in ("para", "list") and not split:
            b.extend(_escape(ln.text), ln)          # wrapped line
        else:
            b.start("para", _escape(ln.text), ln)
 
    b.flush()
    items = b.done + [(t.top, Block("table", _table_to_md(t.rows))) for t in page.tables]
    items.sort(key=lambda x: x[0])
    return [blk for _, blk in items]
 
 
def _continues(prev: str, nxt: str) -> bool:
    """True if `nxt` looks like the continuation of an unfinished sentence in `prev`."""
    return not prev.rstrip().endswith((".", "!", "?", ":", ";", '"', "”")) and nxt[:1].islower()
 
 
def _assemble(pages: list[PageData], body: float, skip: set[str], opts: Options) -> list[Block]:
    blocks: list[Block] = []
    for i, page in enumerate(pages, 1):
        page_blocks = _page_blocks(page, body, skip)
        if opts.page_breaks:
            blocks.append(Block("comment", f"<!-- page {i} -->"))
        elif (
            blocks and page_blocks
            and blocks[-1].kind == "para" and page_blocks[0].kind == "para"
            and _continues(blocks[-1].text, page_blocks[0].text)
        ):
            blocks[-1] = Block("para", _join([blocks[-1].text, page_blocks[0].text]))
            page_blocks = page_blocks[1:]
        blocks.extend(page_blocks)
    return blocks
 
 
def _render(blocks: list[Block]) -> str:
    out: list[str] = []
    prev: Block | None = None
    for blk in blocks:
        if prev is not None:
            out.append("\n" if prev.kind == blk.kind == "list" else "\n\n")
        if blk.kind.startswith("h"):
            out.append("#" * int(blk.kind[1]) + " " + blk.text)
        else:
            out.append(blk.text)
        prev = blk
    return "".join(out).strip() + "\n"
 
 
def _meta(meta: dict, key: str) -> str:
    value = str(meta.get(key) or "").strip()
    return "" if value.lower() in {"(anonymous)", "anonymous", "untitled", "unknown"} else value
 
 
def _front_matter(pdf, path: Path) -> str:
    meta = pdf.metadata or {}
    title = _meta(meta, "Title") or path.stem
    author = _meta(meta, "Author")
    lines = ["---", f"title: {json.dumps(title, ensure_ascii=False)}"]
    if author:
        lines.append(f"author: {json.dumps(author, ensure_ascii=False)}")
    lines += [f"source: {json.dumps(path.name, ensure_ascii=False)}",
              f"pages: {len(pdf.pages)}", "---", "", ""]
    return "\n".join(lines)



# ************************************************Batch driver*****************************************************


@dataclass
class Result:
    src: Path
    dst: Path
    status: str  # ok | exists | no_text | error
    pages: int = 0
    seconds: float = 0.0
    message: str = ""


def convert_one(src: Path, dst: Path, opts: Options, overwrite: bool) -> Result:
    if dst.exists() and not overwrite:
        return Result(src, dst, "exists")
    t0 = time.perf_counter()
    try:
        markdown, pages = pdf_to_markdown(src, opts)
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(markdown, encoding="utf-8")
        return Result(src, dst, "ok", pages, time.perf_counter() - t0)
    except NoTextError as exc:
        return Result(src, dst, "no_text", message=str(exc))
    except Exception as exc:
        msg = f"{type(exc).__name__}: {exc}"
        if "password" in msg.lower():
            msg += "  (use --password)"
        return Result(src, dst, "error", message=msg)


def collect_inputs(inputs: list[str], recursive: bool) -> list[tuple[Path, Path]]:
    """Expand files, directories and glob patterns into (pdf_path, relative_path) pairs."""
    found: list[tuple[Path, Path]] = []
    seen: set[Path] = set()
 
    def add(path: Path, rel: Path) -> None:
        key = path.resolve()
        if key not in seen:
            seen.add(key)
            found.append((path, rel))
 
    for raw in inputs:
        p = Path(raw)
        if p.is_dir():
            for f in sorted(p.rglob("*") if recursive else p.glob("*")):
                if f.is_file() and f.suffix.lower() == ".pdf":
                    add(f, f.relative_to(p))
        elif p.is_file():
            add(p, Path(p.name))
        else:
            matches = [Path(m) for m in sorted(glob.glob(raw, recursive=True))]
            files = [m for m in matches if m.is_file()]
            if not files:
                log.warning("WARN  nothing matches %r", raw)
            for m in files:
                add(m, Path(m.name))
    return found


def plan(inputs: list[str], outdir: Path | None, recursive: bool) -> list[tuple[Path, Path]]:
    """Pair each source PDF with a unique destination .md path."""
    tasks: list[tuple[Path, Path]] = []
    used: set[Path] = set()
    for src, rel in collect_inputs(inputs, recursive):
        base = (outdir / rel if outdir else src).with_suffix(".md")
        dst, n = base, 1
        while dst in used:
            dst = base.with_name(f"{base.stem}_{n}{base.suffix}")
            n += 1
        used.add(dst)
        tasks.append((src, dst))
    return tasks
 

def _report(res: Result, idx: int, total: int) -> None:
    tag = f"[{idx}/{total}]"
    if res.status == "ok":
        log.info("%s OK    %s -> %s  (%d pages, %.1fs)", tag, res.src, res.dst, res.pages, res.seconds)
    elif res.status == "exists":
        log.info("%s SKIP  %s  (%s exists; use --overwrite)", tag, res.src, res.dst)
    elif res.status == "no_text":
        log.warning("%s WARN  %s  %s", tag, res.src, res.message)
    else:
        log.error("%s FAIL  %s  %s", tag, res.src, res.message)
 

def run(tasks, opts: Options, overwrite: bool, jobs: int) -> list[Result]:
    results: list[Result] = []
    total = len(tasks)
    if jobs > 1 and total > 1:
        with ProcessPoolExecutor(max_workers=min(jobs, total)) as pool:
            futures = [pool.submit(convert_one, s, d, opts, overwrite) for s, d in tasks]
            for i, fut in enumerate(as_completed(futures), 1):
                results.append(fut.result())
                _report(results[-1], i, total)
    else:
        for i, (s, d) in enumerate(tasks, 1):
            results.append(convert_one(s, d, opts, overwrite))
            _report(results[-1], i, total)
    return results
 



 