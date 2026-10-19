#!/usr/bin/env python3
"""Convert a PDF into a reflowable EPUB 3 (with EPUB 2 NCX fallback) for any e-reader,
including Kindle via Send to Kindle.

Modes:
  reflow  Extract text into resizable paragraphs. Removes running headers,
          footers and page numbers, re-joins hyphenated words and paragraphs
          split across pages, orders two-column layouts, detects headings
          and chapters, keeps photos, renders vector figures as images and
          converts ruled tables to HTML tables.
  image   Render every page as an image (for scans, comics, math-heavy docs).
  auto    reflow, unless most pages look scanned -> image. (default)

Inspection (no conversion):
  --peek file.pdf   print metadata, outline and the text of the first pages
  --peek file.epub  print the table of contents and a sample chapter's text

Runs fully offline: reads the input file, writes one .epub, nothing else.

Requires: PyMuPDF (pip install "pymupdf>=1.24")
Copyright (C) 2026 Zawanah. Licensed under the GNU AGPL v3.0 (see LICENSE).
"""
import argparse
import contextlib
import datetime
import html
import io
import re
import sys
import uuid
import zipfile
from collections import Counter
from pathlib import Path

try:
    import pymupdf as fitz
except ImportError:  # older PyMuPDF
    try:
        import fitz  # noqa: F401
    except ImportError:
        sys.exit("PyMuPDF is required: pip install pymupdf")

__version__ = "1.1.0"

TEXT_FLAGS =fitz.TEXT_PRESERVE_WHITESPACE | fitz.TEXT_MEDIABOX_CLIP
TERMINAL = set(".!?:;…\"”’')]»")
BULLET_RE = re.compile(r"^\s*([•●▪–\-*·]|\(?\d{1,3}[.)]|\(?[a-z][.)])\s")
PAGENUM_RE = re.compile(r"^(page\s*)?(#|[ivxlcdm]+)(\s*(of|/)\s*#)?$", re.I)
CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ue000-\uf8ff\ufffe\uffff]")  # incl. private-use glyphs
CAPTION_RE = re.compile(r"^(fig(ure)?|table|tab\.|listing|algorithm)\s*\d", re.I)
MATH_CHARS = set("=+−×·∑∏∫√∂∇≤≥≈≠∈∉⊂⊆"
                 "∪∩→←↔∞αβγδεζηθικ"
                 "λμνξπρστυφχψωΓΔΘ"
                 "ΛΞΠΣΦΨΩ")
NOTE_MARK_RE = re.compile(r"^[\d*†‡§¶]{1,4}$")  # footnote reference markers
NAME_RE = re.compile(r"(?:(?:[A-Z][a-z'’-]+|[A-Z]\.)\s){1,3}[A-Z][a-z'’-]+")
GENERIC_TITLES = {"contents", "tableofcontents", "copyright", "index", "preface", "foreword",
                  "introduction", "abouttheauthor", "aboutthebook", "titlepage", "cover", "dedication"}
MAX_IMG_PX = 1400
HYPHENATED = set()  # words seen with an in-line hyphen, e.g. "sequence-aligned"


# --------------------------------------------------------------------------
# helpers

def clean(s):
    return CTRL_RE.sub("", s)


def esc(s):
    return html.escape(clean(s), quote=True)


def norm_key(s):
    return re.sub(r"[^a-z0-9]+", "", s.lower())


def rect_union(rects):
    rects = list(rects)
    return fitz.Rect(min(r.x0 for r in rects), min(r.y0 for r in rects),
                     max(r.x1 for r in rects), max(r.y1 for r in rects))


def frac_inside(r, outer):
    a = r.width * r.height
    if a <= 0:
        return 0.0
    i = fitz.Rect(r) & outer
    return 0.0 if i.is_empty else (i.width * i.height) / a


def parse_pages(spec, n):
    if not spec:
        return list(range(n))
    out = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            a = int(a) if a.strip() else 1
            b = int(b) if b.strip() else n
            out.extend(range(a - 1, min(b, n)))
        else:
            out.append(int(part) - 1)
    return [p for p in dict.fromkeys(out) if 0 <= p < n]


def clean_title(t, fallback):
    t = (t or "").strip()
    t = re.sub(r"^Microsoft (Word|PowerPoint) - ", "", t)
    t = re.sub(r"\.(docx?|pptx?|pdf|tex|indd|odt)$", "", t, flags=re.I)
    if (not t or t.lower() in {"untitled", "title", "unknown"} or norm_key(t) in GENERIC_TITLES
            or re.fullmatch(r"[\w-]+\.\w+", t)):
        return fallback
    return t


def title_from_filename(stem):
    """'Title_ Subtitle -- Author -- Publisher, Year' (shadow-library naming) -> (title, author)."""
    parts = [p.strip() for p in stem.split(" -- ")]
    if len(parts) < 2 or not parts[0] or not parts[1]:
        return None, None
    title = re.sub(r"_ ", ": ", parts[0]).replace("_", " ").strip()
    author = re.sub(r"\s*;\s*", ", ", parts[1])
    return title, author


def smart_case(t):
    if t.upper() != t or not re.search(r"[A-Z]", t):
        return t
    small = {"a", "an", "and", "as", "at", "by", "for", "in", "of", "on", "or", "the", "to", "with"}
    words = t.lower().split()
    return " ".join(w if i and w in small else w[:1].upper() + w[1:] for i, w in enumerate(words))


def title_page(doc, pages):
    """(title, author) from a sparse title page among the first pages, if one exists."""
    for pno in pages[:12]:
        page = doc[pno]
        if not 10 <= len(page.get_text("text").strip()) <= 400:
            continue
        lines = []
        for b in page.get_text("dict", flags=TEXT_FLAGS)["blocks"]:
            for l in b.get("lines", []):
                t = re.sub(r"\s+", " ", "".join(s["text"] for s in l["spans"])).strip()
                if t and abs(l.get("dir", (1, 0))[1]) <= 0.1:
                    lines.append((max(s["size"] for s in l["spans"]), t))
        if not 2 <= len(lines) <= 8:
            continue
        top = max(sz for sz, _ in lines)
        if top < 1.4 * min(sz for sz, _ in lines) and top < 20:
            continue  # nothing stands out: an epigraph or a short note, not a title page
        title = " ".join(t for sz, t in lines if sz >= top * 0.95)
        rest = [(sz, t) for sz, t in lines if sz < top * 0.95]
        sub = rest[0][1] if rest and rest[0][0] >= top * 0.5 and not NAME_RE.fullmatch(rest[0][1]) else None
        author = next((t for _, t in rest if NAME_RE.fullmatch(t)), None)
        title = smart_case(title) + (": " + smart_case(sub) if sub else "")
        if 3 <= len(title) <= 200 and norm_key(title) not in GENERIC_TITLES:
            return title, author
    return None, None


def guess_title(doc, pages):
    """Largest text near the top of the first page that has text."""
    for pno in pages[:3]:
        page = doc[pno]
        spans = []
        for b in page.get_text("dict", flags=TEXT_FLAGS)["blocks"]:
            for l in b.get("lines", []):
                if l["bbox"][1] > page.rect.height * 0.6 or abs(l.get("dir", (1, 0))[1]) > 0.1:
                    continue
                for s in l["spans"]:
                    if s["text"].strip():
                        spans.append((round(s["size"], 1), s["text"]))
        if not spans:
            continue
        top = max(sz for sz, _ in spans)
        body = Counter(sz for sz, _ in spans).most_common(1)[0][0]
        if top < body * 1.3:
            return None
        t = re.sub(r"\s+", " ", " ".join(tx for sz, tx in spans if sz == top)).strip()
        if norm_key(t) in GENERIC_TITLES:
            continue
        return t if 3 <= len(t) <= 150 else None
    return None


def span_style(span):
    font = span.get("font", "")
    f = span.get("flags", 0)
    bold = bool(f & 16) or bool(re.search(r"bold|black|heavy|semibold|demi", font, re.I))
    italic = bool(f & 2) or bool(re.search(r"italic|oblique", font, re.I))
    sup = bool(f & 1)
    mono = bool(f & 8) or bool(re.search(r"mono|courier|consol", font, re.I))
    return (bold, italic, sup, mono)


# --------------------------------------------------------------------------
# pass 1: raw page extraction

def attach_note_markers(blocks):
    """Footnote markers set in a small font become superscripts. A marker the PDF
    stores as its own block is moved into the line it sits on."""
    def raised(s, line):
        size = max(x["size"] for x in line["spans"])
        return (NOTE_MARK_RE.match(s["text"].strip()) and s["size"] <= 0.75 * size
                and (s["bbox"][1] + s["bbox"][3]) / 2 < (line["bbox"].y0 + line["bbox"].y1) / 2)

    for b in blocks:
        for l in b["lines"]:
            for s in l["spans"]:
                if raised(s, l):
                    s["flags"] = s.get("flags", 0) | 1
    # a marker that starts a line ('¹ A decade later') belongs to the line ending just before it
    for b in blocks:
        for l in b["lines"]:
            s = l["spans"][0]
            if len(l["spans"]) < 2 or not s.get("flags", 0) & 1 or not NOTE_MARK_RE.match(s["text"].strip()):
                continue
            m = fitz.Rect(s["bbox"])
            host = next((x for o in blocks for x in o["lines"] if x is not l
                         and abs(x["bbox"].x1 - m.x0) < 3 and x["bbox"].y0 - 2 <= m.y0 <= x["bbox"].y1), None)
            if host:
                host["spans"].append(l["spans"].pop(0))
                host["bbox"] = rect_union([host["bbox"], m])
                l["bbox"] = rect_union(fitz.Rect(x["bbox"]) for x in l["spans"])
    for b in blocks:
        b["bbox"] = rect_union(l["bbox"] for l in b["lines"])
        b["text"] = " ".join("".join(s["text"] for s in l["spans"]).strip() for l in b["lines"])
    kept = []
    for b in blocks:
        if len(b["lines"]) == 1 and NOTE_MARK_RE.match(b["text"]):
            m, mspan = b["bbox"], b["lines"][0]["spans"]
            best = None
            for o in blocks:
                if o is b:
                    continue
                for l in o["lines"]:
                    r = l["bbox"]
                    if (r.y0 - 2 <= m.y0 and m.y1 <= r.y1 + 1 and r.x0 - 2 <= m.x0 <= r.x1 + 0.6 * r.height
                            and all(raised(s, {"spans": l["spans"] + [s], "bbox": rect_union([r, m])}) for s in mspan)):
                        d = abs(m.x0 - r.x1)
                        if best is None or d < best[0]:
                            best = (d, o, l)
            if best:
                _, o, l = best
                for s in mspan:
                    s["flags"] = s.get("flags", 0) | 1
                at = sum(1 for s in l["spans"] if s["bbox"][0] < m.x0)
                l["spans"][at:at] = mspan
                l["bbox"] = rect_union([l["bbox"], m])
                o["bbox"] = rect_union([o["bbox"], m])
                o["text"] = " ".join("".join(s["text"] for s in x["spans"]).strip() for x in o["lines"])
                continue
        kept.append(b)
    return kept


def extract_page(page, want_figures=True, want_tables=True):
    W, H = page.rect.width, page.rect.height
    page_area = W * H
    d = page.get_text("dict", flags=TEXT_FLAGS)
    blocks = []
    for b in d["blocks"]:
        if b.get("type") != 0:
            continue
        lines = []
        for ln in b["lines"]:
            dx, dy = ln.get("dir", (1, 0))
            if abs(dy) > 0.1 or dx < 0:  # rotated text: watermarks, margin stamps
                continue
            spans = [s for s in ln["spans"] if s["text"]]
            if not spans or not "".join(s["text"] for s in spans).strip():
                continue
            lines.append({"bbox": fitz.Rect(ln["bbox"]), "spans": spans})
        if lines:
            blocks.append({
                "bbox": rect_union(l["bbox"] for l in lines),
                "lines": lines,
                "text": " ".join("".join(s["text"] for s in l["spans"]).strip() for l in lines),
            })
    blocks = attach_note_markers(blocks)

    images = []
    for info in page.get_image_info(hashes=True):
        r = fitz.Rect(info["bbox"]) & page.rect
        if r.is_empty or r.width < 30 or r.height < 30:
            continue
        images.append({"bbox": r, "digest": info.get("digest"), "width": info.get("width", 0)})

    tables, figures = [], []
    drawings = []
    if want_figures or want_tables:
        try:
            drawings = page.get_drawings()
        except Exception:
            drawings = []
    if want_tables and sum(1 for dr in drawings if dr.get("items")) >= 4:
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                found = page.find_tables().tables
            for t in found:
                rows = t.extract()
                if len(rows) >= 2 and max(len(r) for r in rows) >= 2:
                    tables.append({"bbox": fitz.Rect(t.bbox), "rows": rows})
        except Exception:
            pass
    if want_figures and drawings:
        keep = []
        for dr in drawings:
            r = fitz.Rect(dr["rect"])
            if r.width * r.height > 0.85 * page_area:
                continue  # page background / frame
            if dr.get("fill") in ((1, 1, 1), [1, 1, 1]) and not dr.get("color"):
                continue  # white masks
            keep.append(dr)
        clusters = []
        if keep:
            try:
                clusters = page.cluster_drawings(drawings=keep, x_tolerance=10, y_tolerance=10)
            except Exception:
                clusters = []
        for c in clusters:
            c = fitz.Rect(c) & page.rect
            if c.is_empty or c.width < 60 or c.height < 40:
                continue
            area = c.width * c.height
            if area < 0.015 * page_area or area > 0.9 * page_area:
                continue
            if any(frac_inside(c, t["bbox"]) > 0.5 for t in tables):
                continue
            inner_chars = sum(len(b["text"]) for b in blocks if frac_inside(b["bbox"], c) > 0.8)
            if inner_chars > 350:  # shaded text box / sidebar, keep as text
                continue
            figures.append({"bbox": c})

    # booktabs-style tables: >=2 horizontal rules spanning the same x-range
    if want_tables and drawings:
        rules = {}
        for dr in drawings:
            for item in dr.get("items", []):
                if item[0] == "l":
                    p1, p2 = item[1], item[2]
                    r = fitz.Rect(min(p1.x, p2.x), min(p1.y, p2.y), max(p1.x, p2.x), max(p1.y, p2.y))
                elif item[0] == "re":
                    r = fitz.Rect(item[1])
                else:
                    continue
                if r.height <= 2 and r.width >= 80:
                    rules.setdefault((round(r.x0 / 3), round(r.x1 / 3)), []).append(r)
        for rs in rules.values():
            if len(rs) < 2:
                continue
            region = rect_union(rs)
            if region.height < 20 or region.height > 0.6 * H:
                continue
            if any(frac_inside(region, x["bbox"]) > 0.3 for x in tables + figures):
                continue
            inner = [b for b in blocks if frac_inside(b["bbox"], region) > 0.5]
            if not inner or sum(len(b["text"]) for b in inner) > 1500:
                continue
            figures.append({"bbox": rect_union([region] + [b["bbox"] for b in inner]), "ruled": True})

    # absorb text that belongs to figures/tables, and images inside figures
    remaining = []
    for b in blocks:
        absorbed = False
        for t in tables:
            if frac_inside(b["bbox"], t["bbox"]) > 0.5:
                absorbed = True
                break
        if not absorbed and not CAPTION_RE.match(b["text"]) and len(b["text"]) < 200:
            for f in figures:
                pad = 1 if f.get("ruled") else 12
                grown = fitz.Rect(f["bbox"]) + (-pad, -pad, pad, pad)
                if frac_inside(b["bbox"], grown) > 0.5:
                    f["bbox"] = rect_union([f["bbox"], b["bbox"]])
                    absorbed = True
                    break
        if not absorbed:
            remaining.append(b)
    images = [im for im in images
              if not any(frac_inside(im["bbox"], f["bbox"]) > 0.6 for f in figures)
              and not any(frac_inside(im["bbox"], t["bbox"]) > 0.6 for t in tables)]
    text_chars = sum(len(b["text"]) for b in remaining)
    # full-page background image behind real text
    images = [im for im in images
              if not ((im["bbox"].width * im["bbox"].height) > 0.5 * page_area and text_chars > 300)]
    scanned = text_chars < 30 and any(
        (im["bbox"].width * im["bbox"].height) > 0.5 * page_area for im in images)
    return {"W": W, "H": H, "blocks": remaining, "images": images,
            "figures": figures, "tables": tables, "scanned": scanned}


# --------------------------------------------------------------------------
# paragraphs

def append_runs(runs, new_runs):
    """Append runs to a paragraph, joining like a line break in prose."""
    new_runs = [list(r) for r in new_runs if r[0]]
    if not new_runs:
        return
    new_runs[0][0] = new_runs[0][0].lstrip()
    if runs:
        last = runs[-1]
        last[0] = last[0].rstrip()
        nxt = "".join(r[0] for r in new_runs).lstrip()
        if last[0].endswith("­"):
            last[0] = last[0][:-1]
        elif re.search(r"[^\W\d_]-$", last[0]) and nxt[:1].islower():
            m = re.search(r"(-?)([^\W\d_]+)-$", last[0])
            a = m.group(2).lower()
            b = re.match(r"[^\W\d_]+", nxt)
            # keep the hyphen in known compounds, and mid-chain ("local-to-" + "global")
            if not (m.group(1) or (b and f"{a}-{b.group(0).lower()}" in HYPHENATED)):
                last[0] = last[0][:-1]
        elif last[0] and not last[0].endswith(("/", "—")):
            last[0] += " "
    runs.extend(new_runs)


def para_text(p):
    return re.sub(r"\s+", " ", "".join(r[0] for r in p["runs"]).replace("­", "")).strip()


def new_para(page_idx, line, indented):
    return {"kind": "text", "page": page_idx, "runs": [], "sizes": Counter(),
            "nlines": 0, "lines": [], "starts_indented": indented, "bbox": fitz.Rect(line["bbox"])}


def add_line(p, line, same_visual_line=False):
    runs = [[s["text"], span_style(s)] for s in line["spans"]]
    if same_visual_line and p["runs"]:
        p["runs"][-1][0] = p["runs"][-1][0].rstrip() + " "
        runs[0][0] = runs[0][0].lstrip()
        p["runs"].extend(runs)
        p["lines"][-1] += " " + "".join(r[0] for r in runs)
    else:
        append_runs(p["runs"], runs)
        p["lines"].append("".join(r[0] for r in runs))
        p["nlines"] += 1
    for s in line["spans"]:
        p["sizes"][round(s["size"] * 2) / 2] += len(s["text"].strip())
    p["bbox"] = rect_union([p["bbox"], line["bbox"]])


def block_to_paras(block, page_idx):
    lines = block["lines"]
    left = min(l["bbox"].x0 for l in lines)
    right = max(l["bbox"].x1 for l in lines)
    paras, cur, prev = [], None, None
    for ln in lines:
        sz = max(s["size"] for s in ln["spans"])
        thr = max(0.8 * sz, 6)
        indent = ln["bbox"].x0 - left
        if prev is None:
            cur = new_para(page_idx, ln, indent > thr and len(lines) > 1)
            paras.append(cur)
            add_line(cur, ln)
        elif abs(ln["bbox"].y0 - prev["bbox"].y0) < 2 and ln["bbox"].x0 >= prev["bbox"].x1 - 1:
            add_line(cur, ln, same_visual_line=True)
        else:
            prev_text = "".join(s["text"] for s in prev["spans"]).strip()
            prev_indent = prev["bbox"].x0 - left
            gap = ln["bbox"].y0 - prev["bbox"].y1
            lh = max(prev["bbox"].height, 1)
            start = False
            if gap > 0.7 * lh:
                start = True
            elif indent > thr and prev_indent <= thr and not BULLET_RE.match(prev_text):
                start = True
            elif (len(lines) >= 3 and prev["bbox"].x1 < right - 4 * sz
                  and prev_text[-1:] in TERMINAL):
                start = True
            elif BULLET_RE.match("".join(s["text"] for s in ln["spans"])):
                start = True
            if start:
                cur = new_para(page_idx, ln, indent > thr)
                paras.append(cur)
            add_line(cur, ln)
        prev = ln
    return paras


def is_equation(p):
    t = p["text"]
    if len(t) > 200 or p["nlines"] > 4 or not any(c in MATH_CHARS for c in t):
        return False
    if t.endswith(".") and len(t) > 40 and re.search(r"[a-z]{3,} [a-z]{3,} [a-z]{3,}", t):
        return False  # reads like a sentence
    chars = [c for c in t if not c.isspace()]
    math = sum(1 for c in chars if c in MATH_CHARS) / max(len(chars), 1)
    styled = sum(len(r[0].strip()) for r in p["runs"] if r[1][1] or r[1][2]) / max(len(chars), 1)
    numbered = bool(re.search(r"\(\d{1,3}[a-z]?\)$", t))
    words = len(re.findall(r"[A-Za-z]{4,}", t))
    return (numbered and (math > 0.02 or styled > 0.15)) or math >= 0.08 or (styled >= 0.25 and words <= 4)


def order_items(items, W):
    """Reading order with two-column detection."""
    mid, tol = W / 2, W * 0.03
    left, right, full = [], [], []
    for it in items:
        b = it["bbox"]
        if b.x1 <= mid + tol:
            left.append(it)
        elif b.x0 >= mid - tol:
            right.append(it)
        else:
            full.append(it)
    chars = lambda col: sum(len(it.get("text", "")) for it in col)
    if len(left) >= 2 and len(right) >= 2 and chars(left) > 150 and chars(right) > 150:
        out = []
        full.sort(key=lambda it: it["bbox"].y0)
        pending_l = sorted(left, key=lambda it: it["bbox"].y0)
        pending_r = sorted(right, key=lambda it: it["bbox"].y0)
        for f in full + [None]:
            cut = f["bbox"].y0 if f else float("inf")
            cy = lambda it: (it["bbox"].y0 + it["bbox"].y1) / 2
            out += [it for it in pending_l if cy(it) < cut]
            out += [it for it in pending_r if cy(it) < cut]
            pending_l = [it for it in pending_l if cy(it) >= cut]
            pending_r = [it for it in pending_r if cy(it) >= cut]
            if f:
                out.append(f)
        return out
    return sorted(items, key=lambda it: (round(it["bbox"].y0), it["bbox"].x0))


# --------------------------------------------------------------------------
# reflow conversion

def build_reflow(doc, pages, opts, log):
    raw = {}
    for i, pno in enumerate(pages):
        raw[pno] = extract_page(doc[pno], not opts.no_figures, not opts.no_tables)
        if (i + 1) % 50 == 0:
            log(f"  extracted {i + 1}/{len(pages)} pages")
    stats = Counter()
    stats["scanned_pages"] = sum(1 for r in raw.values() if r["scanned"])

    size_count = Counter()
    for r in raw.values():
        for b in r["blocks"]:
            b["max_size"] = max(s["size"] for l in b["lines"] for s in l["spans"])
            for l in b["lines"]:
                for s in l["spans"]:
                    size_count[round(s["size"] * 2) / 2] += len(s["text"].strip())
    body = size_count.most_common(1)[0][0] if size_count else 10.0

    # running headers / footers / page numbers: short blocks in the page
    # margins, or the top/bottom-most block near the edge, that repeat
    def margin_blocks(r):
        H, bl = r["H"], r["blocks"]
        edge = set()
        if bl:
            top = min(bl, key=lambda b: b["bbox"].y0)
            bot = max(bl, key=lambda b: b["bbox"].y1)
            if top["bbox"].y1 <= H * 0.2:
                edge.add(id(top))
            if bot["bbox"].y0 >= H * 0.8:
                edge.add(id(bot))
        return [b for b in bl if len(b["text"]) <= 120 and b["max_size"] <= body * 1.15 and (
            id(b) in edge or b["bbox"].y1 <= H * 0.09 or b["bbox"].y0 >= H * 0.91)]

    hf_key = lambda b: re.sub(r"[\d\ue000-\uf8ff]+", "#", b["text"].lower()).strip()
    key_pages = Counter()
    for r in raw.values():
        key_pages.update({hf_key(b) for b in margin_blocks(r)})
    repeat_min = 3 if len(pages) >= 4 else 10 ** 9
    for pno, r in raw.items():
        drop = {id(b) for b in margin_blocks(r)
                if PAGENUM_RE.match(hf_key(b)) or key_pages[hf_key(b)] >= repeat_min}
        stats["header_footer_removed"] += len(drop)
        kept = [b for b in r["blocks"] if id(b) not in drop]
        r["blocks"] = kept
        for b in kept:
            for chain in re.findall(r"[^\W\d_]+(?:-[^\W\d_]+)+", b["text"].lower()):
                parts = chain.split("-")
                HYPHENATED.update(f"{x}-{y}" for x, y in zip(parts, parts[1:]))

    # repeated images (logos)
    digest_pages = Counter()
    for r in raw.values():
        digest_pages.update({im["digest"] for im in r["images"] if im["digest"]})

    # cover page detection: drop page-1 content if it's just a cover
    skip_first = False
    if opts.cover_from_page and pages and pages[0] == 0:
        first = raw[0]
        if sum(len(b["text"]) for b in first["blocks"]) < 100:
            skip_first = True

    items = []
    for pno in pages:
        if skip_first and pno == 0:
            continue
        r = raw[pno]
        page_items = []
        for b in r["blocks"]:
            page_items.append({"kind": "block", "bbox": b["bbox"], "block": b, "text": b["text"]})
        for im in r["images"]:
            if im["digest"] and digest_pages[im["digest"]] >= 3 and len(pages) >= 4:
                stats["repeated_images_skipped"] += 1
                continue
            page_items.append({"kind": "image", "bbox": im["bbox"], "native_w": im["width"], "page": pno})
        for f in r["figures"]:
            page_items.append({"kind": "figure", "bbox": f["bbox"], "page": pno})
        for t in r["tables"]:
            page_items.append({"kind": "table", "bbox": t["bbox"], "rows": t["rows"], "page": pno})
        for it in order_items(page_items, r["W"]):
            if it["kind"] == "block":
                items.extend(block_to_paras(it["block"], pno))
            else:
                items.append(it)

    for p in items:
        if p["kind"] == "text":
            p["size"] = p["sizes"].most_common(1)[0][0] if p["sizes"] else body
            styles = [r[1] for r in p["runs"] if r[0].strip()]
            p["bold_all"] = bool(styles) and all(s[0] for s in styles)
            p["mono_all"] = bool(styles) and all(s[3] for s in styles)
            p["text"] = para_text(p)
            p["heading"] = None
    items = [p for p in items if p["kind"] != "text" or p["text"]]

    # drop caps: a lone big letter followed by the paragraph it starts
    out = []
    for i, p in enumerate(items):
        nxt = items[i + 1] if i + 1 < len(items) else None
        if (p["kind"] == "text" and len(p["text"]) <= 2 and p["text"][:1].isalpha()
                and p["size"] >= body * 1.5 and nxt and nxt["kind"] == "text"):
            nxt["runs"].insert(0, [p["text"], (False, False, False, False)])
            nxt["text"] = p["text"] + nxt["text"]
            continue
        out.append(p)
    items = out

    # heading classification
    texts = [p for p in items if p["kind"] == "text"]
    size_keys = Counter()
    for p in texts:
        if (p["size"] >= body * 1.18 and p["nlines"] <= 4 and len(p["text"]) <= 160):
            size_keys[p["size"]] += 1
    big_sizes = sorted((s for s, c in size_keys.items() if c <= max(3, 0.4 * len(texts))), reverse=True)
    size_level = {s: min(i + 1, 3) for i, s in enumerate(big_sizes)}
    bold_level = min(len(set(size_level.values())) + 1, 3) if size_level else 2
    for p in texts:
        t = p["text"]
        if p["size"] in size_level and p["nlines"] <= 4 and len(t) <= 160 and not (t.endswith(".") and len(t) > 60):
            p["heading"] = size_level[p["size"]]
        elif (p["bold_all"] and p["size"] >= body * 0.95 and len(t) <= 90 and p["nlines"] <= 2
              and t[-1:] not in ".,;" and not t[:1].islower() and not CAPTION_RE.match(t)):
            p["heading"] = bold_level
        p["small"] = p["heading"] is None and p["size"] < body * 0.92
        p["eq"] = p["heading"] is None and not opts.no_figures and is_equation(p)

    # display equations -> rendered images (adjacent equation lines grouped)
    out = []
    for p in items:
        prev = out[-1] if out else None
        if p["kind"] == "text" and p["eq"]:
            if (prev and prev.get("eq_fig") and prev["page"] == p["page"]
                    and p["bbox"].y0 - prev["bbox"].y1 < 2 * p["size"]
                    and min(p["bbox"].x1, prev["bbox"].x1) > max(p["bbox"].x0, prev["bbox"].x0)):
                prev["bbox"] = rect_union([prev["bbox"], p["bbox"]])
            else:
                out.append({"kind": "figure", "eq_fig": True, "page": p["page"], "bbox": fitz.Rect(p["bbox"]),
                            "col_w": raw[p["page"]]["W"]})
                stats["equations_as_images"] += 1
            continue
        out.append(p)
    items = out

    # join paragraphs split across pages / columns / blocks, looking past
    # footnotes, captions and figures that interrupt a paragraph
    def is_float(x):
        return x["kind"] in ("image", "figure", "table") or (
            x["kind"] == "text" and not x["heading"] and (x["small"] or CAPTION_RE.match(x["text"])))

    out = []
    for p in items:
        j = len(out) - 1
        while j >= 0 and len(out) - 1 - j < 4 and is_float(out[j]):
            j -= 1
        prev = out[j] if j >= 0 else None
        if (prev and p["kind"] == "text" and prev["kind"] == "text" and not is_float(p) and not is_float(prev)
                and not prev["heading"] and not p["heading"] and not prev["mono_all"] and not p["mono_all"]
                and abs(prev["size"] - p["size"]) <= body * 0.12 and not BULLET_RE.match(p["text"])):
            nt = p["text"]
            pt = prev["text"]
            k = len(prev["runs"])
            while k and (prev["runs"][k - 1][1][2] or not prev["runs"][k - 1][0].strip()):
                k -= 1  # ignore trailing footnote markers: 'work.'¹ still ends a sentence
            if k < len(prev["runs"]):
                pt = re.sub(r"\s+", " ", "".join(r[0] for r in prev["runs"][:k])).strip()
            hyphen = bool(re.search(r"[^\W\d_]-$", pt)) and nt[:1].islower()
            tight = (p["page"] == prev["page"] and -2 < p["bbox"].y0 - prev["bbox"].y1 < 0.5 * p["size"]
                     and min(p["bbox"].x1, prev["bbox"].x1) > max(p["bbox"].x0, prev["bbox"].x0))
            cont = (pt[-1:] not in TERMINAL and (
                nt[:1].islower() or (not p["starts_indented"] and (p["page"] != prev["page"] or tight)))
            ) or (pt[-1:] == ";" and nt[:1].islower())
            if hyphen or cont:
                append_runs(prev["runs"], p["runs"])
                prev["lines"] += p["lines"]
                prev["nlines"] += p["nlines"]
                prev["text"] = para_text(prev)
                prev["bbox"] = rect_union([prev["bbox"], p["bbox"]]) if prev["page"] == p["page"] else prev["bbox"]
                stats["paragraphs_joined"] += 1
                continue
        out.append(p)
    items = out

    # merge consecutive headings on the same page ("Chapter 1" + "The Title")
    out = []
    for p in items:
        prev = out[-1] if out else None
        if (prev and p["kind"] == "text" and prev["kind"] == "text" and p["heading"] and prev["heading"]
                and p["page"] == prev["page"] and len(prev["text"]) + len(p["text"]) <= 200
                and p["bbox"].y0 - prev["bbox"].y1 < 2.5 * max(p["size"], prev["size"])):
            prev["runs"].append(["\n", (False, False, False, False)])
            prev["runs"].extend(p["runs"])
            prev["text"] = prev["text"] + ": " + p["text"] if not prev["text"].endswith(":") else prev["text"] + " " + p["text"]
            prev["heading"] = min(prev["heading"], p["heading"])
            continue
        out.append(p)
    items = out

    # navigation: PDF outline if present, else detected headings
    toc = [(lvl, title.strip(), pg) for lvl, title, pg in doc.get_toc(simple=True)
           if title.strip() and 1 <= pg <= doc.page_count and (pg - 1) in raw]
    if skip_first:
        toc = [e for e in toc if e[2] != 1]
    use_toc = len(toc) >= 2 and not opts.ignore_outline
    if use_toc:
        base = min(l for l, _, _ in toc)
        rel = lambda l: l - base + 1
        max_rel = max(rel(l) for l, _, _ in toc)
        for p in texts:
            if p.get("heading"):
                p["heading"] = min(max_rel + 1, 4)
        by_page = {}
        for idx, it in enumerate(items):
            by_page.setdefault(it.get("page"), []).append(idx)
        used = set()
        markers = []
        for lvl, title, pg in toc:
            tk = norm_key(title)
            hit = None
            for cand_pg in (pg - 1, pg):
                idxs = [i for i in by_page.get(cand_pg, []) if items[i]["kind"] == "text" and i not in used]
                ordered = [i for i in idxs if items[i]["heading"]] + [i for i in idxs if not items[i]["heading"]]
                for i in ordered:
                    pk = norm_key(items[i]["text"])
                    if not tk or len(pk) < 2:
                        continue
                    if len(pk) <= len(tk) * 1.5 + 12 and (pk.startswith(tk) or tk.startswith(pk) and len(pk) >= len(tk) * 0.6 or tk in pk):
                        hit = i
                        break
                if hit is not None:
                    break
            if hit is not None:
                used.add(hit)
                it = items[hit]
                it["heading"] = min(rel(lvl), 3)
                it["nav"] = (rel(lvl), title)
            else:
                markers.append((pg - 1, rel(lvl), title))
        # insert unmatched outline entries as anchors at the top of their page
        for pg0, lv, title in markers:
            pos = next((i for i, it in enumerate(items) if it.get("page") is not None and it["page"] >= pg0), len(items))
            items.insert(pos, {"kind": "marker", "page": pg0, "nav": (lv, title), "text": ""})
        nav_levels = Counter(it["nav"][0] for it in items if it.get("nav"))
    else:
        for p in items:
            if p["kind"] == "text" and p["heading"]:
                p["nav"] = (p["heading"], p["text"])
        nav_levels = Counter(it["nav"][0] for it in items if it.get("nav"))

    split_level = next((l for l in sorted(nav_levels) if nav_levels[l] >= 2), None)
    if not use_toc:
        for it in items:
            if it.get("nav") and split_level and it["nav"][0] > split_level + 1:
                it["nav"] = None
    stats["body_font_size"] = body
    stats["outline_used"] = use_toc
    return items, split_level, stats


def runs_html(runs):
    merged = []
    for text, style in runs:
        text = clean(text).replace("­", "")
        if text == "\n":
            merged.append(["\n", None])
            continue
        if not text.strip() and merged and merged[-1][1] is not None:
            style = merged[-1][1]
        if merged and merged[-1][1] == style:
            merged[-1][0] += text
        else:
            merged.append([text, style])
    out = []
    for text, style in merged:
        if style is None:
            out.append("<br/>")
            continue
        t = esc(re.sub(r"\s+", " ", text))
        if not t:
            continue
        bold, italic, sup, mono = style
        lead = t[: len(t) - len(t.lstrip())]
        trail = t[len(t.rstrip()):]
        core = t.strip()
        if core:
            if mono:
                core = f"<code>{core}</code>"
            if sup:
                core = f"<sup>{core}</sup>"
            if italic:
                core = f"<em>{core}</em>"
            if bold:
                core = f"<b>{core}</b>"
        out.append(lead + core + trail)
    return re.sub(r"\s+", " ", "".join(out)).strip()


class ImageStore:
    def __init__(self, doc, gray):
        self.doc, self.gray, self.files = doc, gray, []

    def render(self, page_no, rect, kind, native_w=0):
        page = self.doc[page_no]
        w = max(rect.width, 1)
        if kind == "figure":
            zoom = min(3.0, MAX_IMG_PX / w)
        else:
            zoom = min(max(native_w / w if native_w else 2.0, 1.5), 4.0, MAX_IMG_PX / w)
        cs = fitz.csGRAY if self.gray else fitz.csRGB
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), clip=rect, colorspace=cs, alpha=False)
        if kind == "figure":
            data, ext = pix.tobytes("png"), "png"
            if len(data) > 600_000:
                data, ext = pix.tobytes("jpeg", jpg_quality=85), "jpg"
        else:
            data, ext = pix.tobytes("jpeg", jpg_quality=82), "jpg"
        name = f"images/img{len(self.files) + 1:04d}.{ext}"
        self.files.append((name, data))
        return name


def item_html(it, images, prev_kind):
    k = it["kind"]
    if k == "text":
        if it["heading"]:
            lv = it["heading"]
            ident = f' id="{it["id"]}"' if it.get("id") else ""
            runs = [[t, (False,) + st[1:]] if st else [t, st] for t, st in it["runs"]]
            return f"<h{lv}{ident}>{runs_html(runs)}</h{lv}>", "heading"
        if it["mono_all"] and it["nlines"] > 1:
            body = "\n".join(esc(l.rstrip()) for l in it["lines"])
            return f"<pre>{body}</pre>", "pre"
        cls = []
        if it.get("small"):
            cls.append("small")
        if prev_kind in (None, "heading", "image", "table", "pre", "marker"):
            cls.append("first")
        c = f' class="{" ".join(cls)}"' if cls else ""
        ident = f' id="{it["id"]}"' if it.get("id") else ""
        return f"<p{c}{ident}>{runs_html(it['runs'])}</p>", "p"
    if k in ("image", "figure"):
        if it.get("eq_fig"):
            r = fitz.Rect(it["bbox"]) + (-2, -2, 2, 2)
            src = images.render(it["page"], r, "figure")
            pct = max(15, min(100, round(r.width / (0.7 * it["col_w"]) * 100)))
            return f'<div class="eq"><img src="{src}" alt="equation" style="width:{pct}%"/></div>', "eq"
        src = images.render(it["page"], it["bbox"], k, it.get("native_w", 0))
        return f'<div class="img"><img src="{src}" alt=""/></div>', "image"
    if k == "table":
        rows = []
        for ri, row in enumerate(it["rows"]):
            tag = "th" if ri == 0 else "td"
            cells = "".join("<%s>%s</%s>" % (tag, esc(" ".join((c or "").split())), tag) for c in row)
            rows.append(f"<tr>{cells}</tr>")
        return f"<table>{''.join(rows)}</table>", "table"
    if k == "marker":
        return f'<div id="{it["id"]}"></div>', "marker"
    return "", prev_kind


def chapters_from_items(items, split_level, images, book_title, pages_per_chunk=30):
    chapters, cur = [], None
    nid = 0
    chunk_start = None

    def start(title):
        nonlocal cur
        cur = {"title": title, "html": [], "nav": [], "prev": None}
        chapters.append(cur)

    for it in items:
        nav = it.get("nav")
        is_split = bool(nav) and split_level is not None and nav[0] <= split_level
        page = it.get("page")
        if split_level is None and page is not None and it["kind"] == "text":
            if chunk_start is None:
                chunk_start = page
            elif page - chunk_start >= pages_per_chunk:
                start(book_title)
                chunk_start = page
        if cur is None or (is_split and cur["html"]):
            start(nav[1] if nav else book_title)
        if nav:
            nid += 1
            it["id"] = f"n{nid}"
            cur["nav"].append((nav[0], nav[1], it["id"]))
        h, cur["prev"] = item_html(it, images, cur["prev"])
        if h:
            cur["html"].append(h)
    return [c for c in chapters if c["html"]]


# --------------------------------------------------------------------------
# image mode

def build_image_mode(doc, pages, opts, images):
    toc = {}
    for lvl, title, pg in doc.get_toc(simple=True):
        toc.setdefault(pg - 1, []).append((lvl, title.strip()))
    chapters = []
    dpi = opts.dpi
    cs = fitz.csGRAY if opts.grayscale else fitz.csRGB
    for i, pno in enumerate(pages):
        page = doc[pno]
        zoom = min(dpi / 72, MAX_IMG_PX * 1.3 / max(page.rect.width, 1))
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), colorspace=cs, alpha=False)
        name = f"images/page{pno + 1:04d}.jpg"
        images.files.append((name, pix.tobytes("jpeg", jpg_quality=opts.jpeg_quality)))
        nav = [(l, t, f"p{pno + 1}") for l, t in toc.get(pno, []) if t]
        if not toc and i % 10 == 0:
            nav = [(1, f"Page {pno + 1}", f"p{pno + 1}")]
        chapters.append({"title": f"Page {pno + 1}", "nav": nav,
                         "html": [f'<div class="page" id="p{pno + 1}"><img src="{name}" alt="Page {pno + 1}"/></div>']})
    return chapters


# --------------------------------------------------------------------------
# EPUB writer

CSS = """body { margin: 0 1.5%; }
p { margin: 0; text-indent: 1.2em; text-align: justify; }
p.first { text-indent: 0; }
p.small { font-size: 0.85em; }
h1, h2, h3, h4 { text-align: left; page-break-after: avoid; text-indent: 0; }
h1 { font-size: 1.6em; margin: 1.2em 0 0.8em; }
h2 { font-size: 1.3em; margin: 1.1em 0 0.6em; }
h3 { font-size: 1.1em; margin: 1em 0 0.4em; }
h4 { font-size: 1em; margin: 0.8em 0 0.3em; }
div.img { text-align: center; margin: 0.8em 0; page-break-inside: avoid; }
div.img img { max-width: 100%; }
div.eq { text-align: center; margin: 0.5em 0; page-break-inside: avoid; }
div.page { text-align: center; margin: 0; padding: 0; }
div.page img { max-width: 100%; max-height: 100%; }
table { border-collapse: collapse; margin: 0.8em 0; font-size: 0.85em; }
th, td { border: 1px solid #888; padding: 0.15em 0.35em; vertical-align: top; }
pre { white-space: pre-wrap; font-size: 0.8em; margin: 0.6em 0; }
sup { font-size: 0.7em; }
"""


def xhtml(title, body, lang):
    return (f'<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE html>\n'
            f'<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" '
            f'xml:lang="{lang}" lang="{lang}">\n<head>\n<meta charset="utf-8"/>\n<title>{esc(title)}</title>\n'
            f'<link rel="stylesheet" type="text/css" href="style.css"/>\n</head>\n<body>\n{body}\n</body>\n</html>\n')


def nav_tree(flat):
    """flat [(level, title, href)] -> nested [(title, href, children)]"""
    root = []
    stack = [(0, root)]
    for lv, title, href in flat:
        while len(stack) > 1 and stack[-1][0] >= lv:
            stack.pop()
        node = (title, href, [])
        stack[-1][1].append(node)
        stack.append((lv, node[2]))
    return root


def write_epub(out_path, meta, chapters, images, cover, lang):
    book_id = f"urn:uuid:{meta['uuid']}"
    files = []  # (path in OEBPS, id, media-type, data, properties)
    files.append(("style.css", "css", "text/css", CSS.encode(), None))
    spine = []
    if cover:
        files.append(("images/cover.jpg", "cover-image", "image/jpeg", cover, "cover-image"))
        files.append(("cover.xhtml", "cover", "application/xhtml+xml",
                      xhtml("Cover", '<div class="page"><img src="images/cover.jpg" alt="Cover"/></div>', lang).encode(), None))
        spine.append("cover")
    flat_nav = []
    for i, ch in enumerate(chapters, 1):
        fname = f"ch{i:04d}.xhtml"
        files.append((fname, f"ch{i:04d}", "application/xhtml+xml",
                      xhtml(ch["title"], "\n".join(ch["html"]), lang).encode("utf-8"), None))
        spine.append(f"ch{i:04d}")
        for lv, title, anchor in ch["nav"]:
            flat_nav.append((lv, title, f"{fname}#{anchor}"))
        if not ch["nav"] and i == 1:
            flat_nav.append((1, ch["title"] or meta["title"], fname))
    if not flat_nav:
        flat_nav = [(1, meta["title"], "ch0001.xhtml")]
    if flat_nav:
        base = min(l for l, _, _ in flat_nav)
        flat_nav = [(l - base + 1, (t[:117] + "...") if len(t) > 120 else t, h) for l, t, h in flat_nav]
    for name, data in images.files:
        mt = "image/png" if name.endswith(".png") else "image/jpeg"
        files.append((name, re.sub(r"\W", "_", name), mt, data, None))
    tree = nav_tree(flat_nav)

    def nav_ol(nodes):
        lis = []
        for t, h, kids in nodes:
            inner = nav_ol(kids) if kids else ""
            lis.append(f'<li><a href="{h}">{esc(t)}</a>{inner}</li>')
        return f"<ol>{''.join(lis)}</ol>"

    nav_body = f'<nav epub:type="toc" id="toc"><h1>Contents</h1>{nav_ol(tree)}</nav>'
    files.append(("nav.xhtml", "nav", "application/xhtml+xml", xhtml("Contents", nav_body, lang).encode(), "nav"))

    order = [0]

    def ncx_points(nodes):
        out = []
        for t, h, kids in nodes:
            order[0] += 1
            out.append(f'<navPoint id="np{order[0]}" playOrder="{order[0]}"><navLabel><text>{esc(t)}</text></navLabel>'
                       f'<content src="{h}"/>{ncx_points(kids)}</navPoint>')
        return "".join(out)

    def depth(nodes):
        return 1 + max((depth(k) for _, _, k in nodes), default=0) if nodes else 0

    ncx = (f'<?xml version="1.0" encoding="utf-8"?>\n<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">'
           f'<head><meta name="dtb:uid" content="{book_id}"/><meta name="dtb:depth" content="{max(depth(tree), 1)}"/>'
           f'<meta name="dtb:totalPageCount" content="0"/><meta name="dtb:maxPageNumber" content="0"/></head>'
           f'<docTitle><text>{esc(meta["title"])}</text></docTitle><navMap>{ncx_points(tree)}</navMap></ncx>')
    files.append(("toc.ncx", "ncx", "application/x-dtbncx+xml", ncx.encode(), None))

    modified = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    manifest = "\n".join(
        f'<item id="{fid}" href="{p}" media-type="{mt}"' + (f' properties="{pr}"' if pr else "") + "/>"
        for p, fid, mt, _, pr in files)
    spine_xml = "\n".join(f'<itemref idref="{s}"/>' for s in spine)
    cover_meta = '<meta name="cover" content="cover-image"/>' if cover else ""
    opf = (f'<?xml version="1.0" encoding="utf-8"?>\n'
           f'<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid" xml:lang="{lang}">\n'
           f'<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">\n'
           f'<dc:identifier id="bookid">{book_id}</dc:identifier>\n<dc:title>{esc(meta["title"])}</dc:title>\n'
           f'<dc:creator>{esc(meta["author"])}</dc:creator>\n<dc:language>{lang}</dc:language>\n'
           f'<meta property="dcterms:modified">{modified}</meta>\n{cover_meta}\n</metadata>\n'
           f'<manifest>\n{manifest}\n</manifest>\n<spine toc="ncx">\n{spine_xml}\n</spine>\n</package>\n')
    container = ('<?xml version="1.0" encoding="utf-8"?>\n<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                 '<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>')
    with zipfile.ZipFile(out_path, "w") as z:
        z.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml", container, compress_type=zipfile.ZIP_DEFLATED)
        z.writestr("OEBPS/content.opf", opf, compress_type=zipfile.ZIP_DEFLATED)
        for p, _, mt, data, _ in files:
            ct = zipfile.ZIP_STORED if mt.startswith("image/") else zipfile.ZIP_DEFLATED
            z.writestr("OEBPS/" + p, data, compress_type=ct)


# --------------------------------------------------------------------------
# inspection helpers (--peek)

def peek_pdf(doc, page_spec, max_chars=2500):
    md = doc.metadata or {}
    print(f"pages: {doc.page_count}")
    for k in ("title", "author", "subject", "creator", "producer"):
        print(f"{k}: {(md.get(k) or '').strip()}")
    toc = doc.get_toc(simple=True)
    print(f"outline entries: {len(toc)}")
    for lvl, title, pg in toc[:40]:
        print(f"  {'  ' * (lvl - 1)}{title.strip()}  (p.{pg})")
    pages = parse_pages(page_spec, doc.page_count) if page_spec else list(range(min(3, doc.page_count)))
    for pno in pages[:3]:
        text = doc[pno].get_text("text").strip()
        print(f"\n--- page {pno + 1} ({len(text)} chars) ---")
        print(text[:max_chars] if text else "[no text layer: scanned?]")


def peek_epub(path, max_chars=3000):
    with zipfile.ZipFile(path) as z:
        names = sorted(n for n in z.namelist() if re.search(r"/ch\d+\.xhtml$", n))
        if "OEBPS/nav.xhtml" in z.namelist():
            nav = z.read("OEBPS/nav.xhtml").decode("utf-8", "replace")
            entries = [html.unescape(t) for t in re.findall(r"<a [^>]*>(.*?)</a>", nav)]
            print(f"toc entries: {len(entries)}")
            for t in entries[:40]:
                print(f"  {t}")
        print(f"content files: {len(names)}")
        if not names:
            return
        name = names[len(names) // 2]
        body = z.read(name).decode("utf-8", "replace")
        body = re.sub(r"</(p|h\d|div|pre|tr)>", "\n", body.split("<body>", 1)[-1])
        text = html.unescape(re.sub(r"<[^>]+>", "", body))
        print(f"\n--- {name} ---")
        print(re.sub(r"\n\s*\n+", "\n\n", text).strip()[:max_chars])


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="Convert a PDF to a reflowable EPUB for any e-reader.")
    ap.add_argument("pdf", help="input .pdf (or an .epub with --peek)")
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    ap.add_argument("--peek", action="store_true",
                    help="inspect only: PDF metadata/outline/first-page text, or an EPUB's TOC and sample text")
    ap.add_argument("-o", "--output", help="output .epub path (default: next to the PDF)")
    ap.add_argument("--mode", choices=["auto", "reflow", "image"], default="auto")
    ap.add_argument("--title")
    ap.add_argument("--author")
    ap.add_argument("--lang", default="en")
    ap.add_argument("--pages", help='page selection, e.g. "1-200" or "3-,10" (1-based)')
    ap.add_argument("--no-cover", action="store_true", help="don't make a cover from the first page")
    ap.add_argument("--cover", help="image file to use as cover instead of page 1")
    ap.add_argument("--no-figures", action="store_true", help="don't render vector drawings as images")
    ap.add_argument("--no-tables", action="store_true", help="don't detect tables")
    ap.add_argument("--ignore-outline", action="store_true", help="ignore PDF bookmarks; use detected headings")
    ap.add_argument("--grayscale", action="store_true", help="grayscale images (smaller file)")
    ap.add_argument("--dpi", type=int, default=150, help="page render DPI in image mode")
    ap.add_argument("--jpeg-quality", type=int, default=80)
    ap.add_argument("--password", help="password for encrypted PDFs")
    opts = ap.parse_args()

    log = lambda m: print(m, file=sys.stderr)
    src = Path(opts.pdf).expanduser().resolve()
    if not src.exists():
        sys.exit(f"Not found: {src}")
    if opts.peek and src.suffix.lower() == ".epub":
        peek_epub(src)
        return
    doc = fitz.open(src)
    if doc.needs_pass and not (opts.password and doc.authenticate(opts.password)):
        sys.exit("PDF is encrypted; pass --password")
    if opts.peek:
        peek_pdf(doc, opts.pages)
        return
    pages = parse_pages(opts.pages, doc.page_count)
    if not pages:
        sys.exit("No pages selected")
    md = doc.metadata or {}
    fn_title, fn_author = title_from_filename(src.stem)
    tp_title, tp_author = title_page(doc, pages)
    if tp_title and fn_title and not norm_key(tp_title).startswith(norm_key(fn_title)):
        tp_title = None  # the filename names the book; a title page that disagrees is something else
    fallback = tp_title or fn_title or guess_title(doc, pages) or src.stem.replace("_", " ")
    title = opts.title or clean_title(md.get("title"), fallback)
    author = opts.author or (md.get("author") or "").strip() or fn_author or tp_author or "Unknown"
    out = Path(opts.output).expanduser().resolve() if opts.output else src.with_suffix(".epub")

    mode = opts.mode
    if mode == "auto":
        sample = pages[:: max(1, len(pages) // 20)][:20]
        scanned = sum(1 for p in sample if len(doc[p].get_text("text").strip()) < 30)
        mode = "image" if scanned / len(sample) >= 0.6 else "reflow"
        if mode == "image":
            log(f"Auto mode: {scanned}/{len(sample)} sampled pages have no text layer -> image mode")

    cover = None
    if opts.cover:
        pix = fitz.Pixmap(str(Path(opts.cover).expanduser()))
        if pix.alpha or (pix.colorspace and pix.colorspace.n not in (1, 3)):
            pix = fitz.Pixmap(fitz.csRGB, pix, 0)
        cover = pix.tobytes("jpeg", jpg_quality=88)
    elif not opts.no_cover:
        p0 = doc[pages[0]]
        zoom = min(1600 / max(p0.rect.height, 1), 1200 / max(p0.rect.width, 1))
        cover = p0.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False).tobytes("jpeg", jpg_quality=88)
    opts.cover_from_page = cover is not None and not opts.cover

    images = ImageStore(doc, opts.grayscale)
    stats = Counter()
    if mode == "reflow":
        log(f"Reflowing {len(pages)} pages...")
        items, split_level, stats = build_reflow(doc, pages, opts, log)
        chapters = chapters_from_items(items, split_level, images, title)
        if stats["scanned_pages"] > len(pages) * 0.3:
            log(f"WARNING: {stats['scanned_pages']} pages look scanned (no text layer); "
                "they were kept as page images. Consider --mode image or OCR the PDF first.")
    else:
        log(f"Rendering {len(pages)} pages as images...")
        chapters = build_image_mode(doc, pages, opts, images)

    meta = {"title": title, "author": author,
            "uuid": str(uuid.uuid5(uuid.NAMESPACE_URL, f"{src.name}|{title}|{author}|{mode}"))}
    write_epub(out, meta, chapters, images, cover, opts.lang)

    size_mb = out.stat().st_size / 1e6
    nav_count = sum(len(c["nav"]) for c in chapters)
    print(f"OK: {out}")
    print(f"  mode={mode}  pages={len(pages)}  title={title!r}  author={author!r}")
    print(f"  chapters/files={len(chapters)}  toc_entries={nav_count}  images={len(images.files)}  size={size_mb:.1f} MB")
    if mode == "reflow":
        print(f"  body_font={stats['body_font_size']}pt  outline_used={stats['outline_used']}  "
              f"headers_footers_removed={stats['header_footer_removed']}  paragraphs_joined={stats['paragraphs_joined']}  "
              f"scanned_pages={stats['scanned_pages']}")
        print("  toc preview: " + " | ".join(t for c in chapters for _, t, _ in c["nav"][:1])[:600])
    if size_mb > 50:
        print("  NOTE: >50 MB: too big for Send-to-Kindle email; use the web/app uploader (200 MB limit) "
              "or re-run with --grayscale / lower --dpi.")


if __name__ == "__main__":
    main()
