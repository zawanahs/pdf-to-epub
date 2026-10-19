---
name: convert
description: Convert PDF files into reflowable EPUB ebooks for Kindle (via Send to Kindle) and other e-readers, with chapters/TOC, cover, figures, tables and equations. Use when the user wants to read a PDF on a Kindle or e-reader, convert a PDF to EPUB, or make a PDF "Kindle-ready" (novels, books, papers, technical docs).
argument-hint: "<file.pdf | folder> [options]"
allowed-tools: Bash(python ${CLAUDE_PLUGIN_ROOT}/scripts/pdf_to_epub.py *), Bash(python3 ${CLAUDE_PLUGIN_ROOT}/scripts/pdf_to_epub.py *)
---

# PDF → EPUB

Converts a PDF into an EPUB 3 file that Amazon's **Send to Kindle** accepts. The output is
reflowable, so the reader can change the font size. Conversion is done by one bundled
Python script. It runs fully offline: it reads the input PDF and writes one `.epub`.

Script: `${CLAUDE_PLUGIN_ROOT}/scripts/pdf_to_epub.py`

Use `python3` on macOS/Linux and `python` on Windows. The examples below say `python`.

## Workflow

1. **Find the PDF(s)** from `$ARGUMENTS` or the conversation. For a folder, convert each
   `.pdf` in turn.

2. **Check the dependency.** The script needs PyMuPDF. If running the script fails with
   "PyMuPDF is required", ask the user before installing it. Then run:
   `python -m pip install "pymupdf>=1.24"`

3. **Convert.** The output goes next to the PDF with the same name and a `.epub` extension:
   ```bash
   python ${CLAUDE_PLUGIN_ROOT}/scripts/pdf_to_epub.py "path/to/file.pdf"
   ```

4. **Read the report** the script prints and fix what looks off:
   - `title` / `author`: these come from PDF metadata, then a title page in the first pages, then a
     `Title -- Author -- Publisher, Year` filename, then the largest text on page 1.
     If the author is `Unknown` or the title is wrong, inspect the PDF:
     `python ${CLAUDE_PLUGIN_ROOT}/scripts/pdf_to_epub.py --peek "file.pdf"`
     Then re-run with `--title "..." --author "..."`.
   - `toc preview`: should list real chapters or sections. If the PDF's bookmarks are junk,
     re-run with `--ignore-outline` to use detected headings instead.
   - `scanned_pages` > 0 / "WARNING ... scanned": the pages have no text layer. Auto mode already
     switches to image mode when most pages are scans. Tell the user that OCR (for example
     `ocrmypdf`) would give reflowable text.

5. **Spot-check the content** before handing it over. This prints the TOC and a middle chapter:
   ```bash
   python ${CLAUDE_PLUGIN_ROOT}/scripts/pdf_to_epub.py --peek "file.epub"
   ```
   Look for leftover running headers or page numbers, paragraphs broken mid-sentence,
   and text out of order. If the layout is hopeless (heavy math, magazine layouts,
   comics, sheet music), re-run with `--mode image`.

6. **Tell the user** where the file is and how to get it onto their device (below).

## Options

| Flag | Use when |
|---|---|
| `--mode auto\|reflow\|image` | `auto` (default) reflows text and switches to `image` if most pages are scans. `image` renders each page as a picture: faithful, but no font resizing. |
| `--title`, `--author`, `--lang en` | Fix metadata, which the e-reader shows in its library. |
| `--pages "5-300"` | Skip front matter, ads or an index (1-based, e.g. `"1-10,15,20-"`). |
| `-o out.epub` | Choose a different output path. |
| `--cover img.jpg` / `--no-cover` | The default cover is a render of the first selected page. |
| `--ignore-outline` | Use detected headings instead of the PDF's bookmarks for chapters and TOC. |
| `--no-figures` | Spurious images appear (decorative boxes rendered as figures). This also turns equation images off. |
| `--no-tables` | Table detection mangles something. |
| `--grayscale`, `--dpi 120`, `--jpeg-quality 70` | Shrink large files. |
| `--password` | The PDF is encrypted. |
| `--peek` | Inspect a PDF (metadata, outline, first pages) or an EPUB (TOC, sample chapter) without converting. |

## What reflow mode handles

- **Clean-up:** strips running headers, footers and page numbers, including page numbers drawn
  with private-use glyphs, and drops rotated margin text such as arXiv stamps.
- **Hyphenation:** rejoins words hyphenated at line ends, keeping real compounds that appear
  hyphenated elsewhere in the document and the middle of chains like "local-to-global".
- **Split paragraphs:** rejoins paragraphs split across pages, columns and blocks, looking past
  interrupting footnotes and figures.
- **Two-column layouts:** reads them in the correct order.
- **Chapters:** uses PDF bookmarks when present, otherwise headings detected by font size and
  bold. Also handles drop caps and "Chapter 1" + title pairs.
- **Footnote markers:** small reference numbers become superscripts on the word they follow,
  even when the PDF stores them as separate text blocks.
- **Inline formatting:** keeps italic, bold, superscripts and monospace, and turns monospace
  blocks into `<pre>`.
- **Images:** keeps photos and skips logos repeated on every page. Vector figures, display
  equations and booktabs-style tables are rendered as crisp images; ruled tables become HTML
  tables.

**Known limits:** inline math stays as plain text, and complex layouts (magazines, forms) may
reflow imperfectly. Suggest `--mode image` for those.

## Getting it onto a Kindle

Kindles can't open `.epub` copied over USB. Send it through Amazon's Send to Kindle service,
which converts it:
- **Web:** amazon.com/sendtokindle, then drag the file in (up to 200 MB).
- **App:** the Send to Kindle desktop app, or the Kindle phone app's share sheet.
- **Email:** attach it to a message sent to the device's `@kindle.com` address (up to 50 MB). The sender
  must be on the approved list under Amazon → *Manage Your Content and Devices → Preferences →
  Personal Document Settings*.

Other e-readers (Kobo, Apple Books, Google Play Books, etc.) open the `.epub` directly.
