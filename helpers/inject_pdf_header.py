#!/usr/bin/env python3
"""Inject a reusable PDF download header + html2pdf lazy loader into an HTML file.

Features:
- Inserts (if absent) a <script> defining a global downloadPDF() that lazy-loads html2pdf.js
- Injects a fixed header bar with a Download PDF button right after the opening <body> tag.
- Adds a scoped <style> block (if absent) to avoid leaking styles; hides header on print.
- Idempotent: running multiple times will NOT duplicate header or script.
- Optional: choose output path (defaults to in-place rewrite) and customize filename / header label.

Usage:
  python scripts/inject_pdf_header.py input.html
  python scripts/inject_pdf_header.py input.html --output output.html
  python scripts/inject_pdf_header.py input.html --pdf-filename report.pdf --label "Export PDF"

Limitations:
- Uses simple regex heuristics; for extremely malformed HTML you may need a real parser.
- Does not attempt to find or modify a specific content container; the button exports the first element
  matching the selector provided via --selector (default: '.container' else falls back to 'body').

"""
from __future__ import annotations
import argparse
import re
from pathlib import Path

HEADER_MARKER = "<!-- PDF DOWNLOAD HEADER INSERTED -->"
SCRIPT_MARKER = "<!-- PDF DOWNLOAD SCRIPT INSERTED -->"
STYLE_MARKER  = "/* PDF DOWNLOAD STYLE INSERTED */"

STYLE_BLOCK = f"""<style>{STYLE_MARKER}\n.download-header {{position:fixed;top:0;right:0;display:flex;justify-content:flex-end;align-items:center;padding:7px 15px;background:#f5f5f5;border-bottom:1px solid #ccc;z-index:1000;font-family:Open Sans, Arial, sans-serif;box-shadow:0 1px 2px rgba(0,0,0,0.08);}}\n.download-header button {{background:#0061bf;color:#fff;border:none;padding:10px 18px;font-size:14px;border-radius:6px;cursor:pointer;line-height:1.2;display:inline-flex;gap:8px;align-items:center;font-weight:500;transition:all 0.3s ease;position:relative;overflow:hidden;animation:pulseGlow 3s ease-in-out infinite;}}\n.download-header button:hover {{background:#004f98;transform:translateY(-1px);box-shadow:0 4px 12px rgba(0,97,191,0.3);}}\n.download-header button:active {{transform:translateY(0);box-shadow:0 2px 6px rgba(0,97,191,0.2);}}\n.download-header button svg {{transition:transform 0.2s ease;}}\n.download-header button:hover svg {{transform:translateY(1px);}}\n@keyframes pulseGlow {{0%,100% {{box-shadow:0 0 0 0 rgba(0,97,191,0.4);}} 50% {{box-shadow:0 0 0 4px rgba(0,97,191,0.1);}}}}\n@media screen {{ .pdf-offset {{ margin-top:24px; }} }}\n@media print {{ .download-header {{ display:none !important; }} }}\n.pdf-offset, .container {{ padding-bottom: 99px; }}\n</style>"""

SCRIPT_TEMPLATE = """<script>{marker}\nwindow.downloadPDF = function() {{\n  console.log('Opening print dialog...');\n  window.print();\n}};\n</script>"""

HEADER_TEMPLATE = """{marker}\n<div class=\"download-header\">\n  <button onclick=\"downloadPDF()\"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7,10 12,15 17,10"/><line x1="12" y1="15" x2="12" y2="3"/></svg> <span>{label}</span></button>\n</div>"""

BODY_OPEN_RE = re.compile(r"(<body[^>]*>)", re.IGNORECASE)
HEAD_CLOSE_RE = re.compile(r"</head>", re.IGNORECASE)


def already_present(html: str) -> bool:
    return (HEADER_MARKER in html) and (SCRIPT_MARKER in html) and (STYLE_MARKER in html)


def inject(html: str, *, pdf_filename: str, label: str, selector: str) -> str:
    if already_present(html):
        return html  # idempotent

    # Insert style + script before </head> (or at top if no head)
    script_block = SCRIPT_TEMPLATE.format(marker=SCRIPT_MARKER, pdf_filename=pdf_filename, selector=selector)
    head_inserts = STYLE_BLOCK + "\n" + script_block + "\n"
    if HEAD_CLOSE_RE.search(html):
        html = HEAD_CLOSE_RE.sub(head_inserts + "</head>", html, count=1)
    else:
        html = head_inserts + html

    # Insert header right after opening <body>
    header_block = HEADER_TEMPLATE.format(marker=HEADER_MARKER, label=label)
    if BODY_OPEN_RE.search(html):
        html = BODY_OPEN_RE.sub(lambda m: m.group(1) + "\n" + header_block + "\n", html, count=1)
    else:  # no body tag; prepend
        html = header_block + "\n" + html

    # Add pdf-offset class to first element with class="container" if found and not already offset
    def add_offset(match: re.Match) -> str:
        tag = match.group(0)
        if 'pdf-offset' in tag:
            return tag
        # insert pdf-offset into class attribute
        return re.sub(r'class="([^"]*)"', lambda c: f'class="{c.group(1)} pdf-offset"', tag, count=1)

    html = re.sub(r'<(div|section)([^>]*class="[^"]*container[^"]*"[^>]*)>',
                  lambda m: add_offset(m), html, count=1, flags=re.IGNORECASE)
    return html


def main():
    ap = argparse.ArgumentParser(description="Inject a PDF download header + script into an HTML file")
    ap.add_argument('input', type=Path)
    ap.add_argument('--output', type=Path, help='Output file (default: overwrite input)')
    ap.add_argument('--pdf-filename', default='download.pdf', help='Downloaded PDF file name')
    ap.add_argument('--label', default='Download PDF', help='Button label text')
    ap.add_argument('--selector', default='.container', help='CSS selector to export (falls back to body)')
    args = ap.parse_args()

    html = args.input.read_text(encoding='utf-8')
    new_html = inject(html, pdf_filename=args.pdf_filename, label=args.label, selector=args.selector)

    out_path = args.output or args.input
    out_path.write_text(new_html, encoding='utf-8')
    print(f"Injected PDF header into: {out_path}")
    if out_path == args.input:
        print("(In-place update)")


if __name__ == '__main__':
    main()

