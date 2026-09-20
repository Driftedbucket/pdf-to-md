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



