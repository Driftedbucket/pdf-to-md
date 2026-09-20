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
 

# Tunables

PARA_GAP = 0.6   # vertical gap (in line-heights) that starts a new paragraph
MARGIN = 0.08    # top/bottom fraction of the page searched for headers/footers
 
BULLET_RE = re.compile(r"^\s*(?:[•●▪■◦‣∙·*\-–—]|\(cid:\d+\))\s+(?=\S)")
NUMBERED_RE = re.compile(r"^\s*(\d{1,3})[.)]\s+(?=\S)")
LETTERED_RE = re.compile(r"^\s*[a-z]\)\s+(?=\S)")
BOLD_HINTS = ("bold", "black", "heavy", "demi")



# Data structures

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