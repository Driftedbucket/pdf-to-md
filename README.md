# pdf-to-md

Convert PDFs to Markdown.

## Setup

```
pip install pdfplumber
```

## Run

```
python pdf2md.py pdfs -o markdown
```

Converts every PDF in `pdfs/` and writes the `.md` files to `markdown/`.

## Useful options

| Option | What it does |
| --- | --- |
| `-o DIR` | Output folder (default: next to each PDF) |
| `-f` | Overwrite existing `.md` files |
| `-j 0` | Use all CPU cores |
| `-r` | Include sub-folders |
| `--front-matter` | Add title/author/pages header |
| `--page-breaks` | Mark page boundaries |
| `--password PW` | Open encrypted PDFs |

You can also pass single files or patterns: `python pdf2md.py "pdfs/Unit*.pdf"`

Full list: `python pdf2md.py -h`

