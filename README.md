# PDF to EPUB

A Claude Code plugin that turns PDFs into clean, reflowable EPUB ebooks you can read on any
e-reader: Kobo, Apple Books, Google Play Books, or a Kindle via Send to Kindle. Ask Claude to
"convert this PDF to EPUB" and it runs the converter, checks the result, fixes the title and
author if needed, and tells you how to get the file onto your device.

It works best on text-based PDFs such as novels, non-fiction books, academic papers and
technical documents. Scanned PDFs are detected automatically and converted page by page as
images.

## What you get

- **Reflowable text** that you can resize on the device.
- **Clean-up:** running headers, footers and page numbers are removed.
- **Joined text:** words hyphenated at line breaks and paragraphs split across pages are rejoined.
- **Reading order:** two-column layouts, as in many papers, are read in the right order.
- **Chapters and a table of contents**, taken from the PDF's bookmarks or from detected headings.
- **Formatting:** italic, bold, superscripts and code are kept.
- **Images:** photos and figures are kept, logos repeated on every page are dropped, and
  display equations and tables are rendered as sharp images.
- **A cover image**, rendered from the first page.
- **Page-image mode** for scans, comics or math-heavy documents.

## Install

From Claude Code:

```
/plugin marketplace add zawanahs/pdf-to-epub
/plugin install pdf-to-epub@zawanahs
```

Or from a shell:

```bash
claude plugin marketplace add zawanahs/pdf-to-epub
claude plugin install pdf-to-epub@zawanahs
```

### Requirements

You need Python 3.8 or newer and the [PyMuPDF](https://pymupdf.readthedocs.io/) library. If
PyMuPDF is missing, Claude asks before installing it with `python -m pip install "pymupdf>=1.24"`.

## Usage

Ask in plain language, for example "convert ~/Downloads/book.pdf to epub", or "make every PDF
in this folder Kindle-ready". You can also invoke the skill directly:

```
/pdf-to-epub:convert path/to/book.pdf
```

The `.epub` is saved next to the original PDF. The script can also be run on its own:

```bash
python scripts/pdf_to_epub.py book.pdf                      # convert
python scripts/pdf_to_epub.py book.pdf --pages 5-300 --author "Jane Austen"
python scripts/pdf_to_epub.py scan.pdf --mode image --grayscale
python scripts/pdf_to_epub.py book.pdf --peek               # inspect without converting
python scripts/pdf_to_epub.py --help                        # all options
```

### Getting the file onto an e-reader

Kobo, Apple Books, Google Play Books and most other readers open the `.epub` directly.

**Kindle** doesn't open `.epub` files copied over USB. Use Amazon's Send to Kindle instead,
which converts the file:

- **Web:** amazon.com/sendtokindle (files up to 200 MB).
- **App:** the Send to Kindle desktop app, or the Kindle phone app's share sheet.
- **Email:** send the file to your device's `@kindle.com` address (up to 50 MB). The sending
  address must be approved in your Amazon account settings.

## What this plugin runs, reads and sends

- **Bundled code:** one skill (`skills/convert/SKILL.md`) and one readable Python script
  (`scripts/pdf_to_epub.py`). There are no hooks, MCP servers or background processes.
- **Pre-approved command:** the skill pre-approves only that script
  (`python …/scripts/pdf_to_epub.py`) so conversions don't prompt for permission each time.
- **Files touched:** the script reads the PDF you point it at and writes a single `.epub`.
  By default the `.epub` goes next to the PDF, or wherever `-o` says.
- **Network:** none. The script makes no network requests and collects or sends no data.
- **Installs:** the only install is the PyMuPDF dependency described above, and Claude asks you
  first.

## Limitations

- Math written inside a sentence stays as plain text. Display equations become images.
- Magazine-style layouts, forms and heavily designed pages may not reflow perfectly. Use
  `--mode image` for them.
- Scanned PDFs have no text layer, so they become page images. Run OCR first (for example with
  `ocrmypdf`) if you want reflowable text.
- The converter has no OCR built in, and it doesn't remove DRM.

## License

Copyright (C) 2026 Zawanah.

This plugin is licensed under the GNU Affero General Public License v3.0 or later; see
[LICENSE](LICENSE). It uses [PyMuPDF](https://github.com/pymupdf/PyMuPDF), which is also
licensed under the AGPL-3.0 (commercial licenses are available from Artifex).

"Kindle" is a trademark of Amazon.com, Inc. This project is not affiliated with or endorsed by
Amazon.
