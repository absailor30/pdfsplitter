# pdfsplit

Split very large PDFs (tested design target: 2.5–3 GB) into smaller files.

Built on [pikepdf](https://pikepdf.readthedocs.io) (qpdf). The source is
memory-mapped and pages are copied lazily, so peak RAM tracks the chunk being
written, not the size of the input.

## Install

    pip install -r requirements.txt

## Usage

    python pdfsplit.py big.pdf --pages 500          # 500 pages per file
    python pdfsplit.py big.pdf --parts 8            # 8 roughly equal parts
    python pdfsplit.py big.pdf --size 100           # ~100 MB per file
    python pdfsplit.py big.pdf --ranges '1-100,101-'
    python pdfsplit.py big.pdf --pages 500 --dry-run

Options: `-o/--out-dir`, `--prefix`, `--password`, `--linearize`, `-q/--quiet`.
Default output dir is `<input>_split` next to the input.

## Notes

- `--size` is an estimate, not exact. It sums each page's referenced stream
  lengths (shared objects counted once per chunk), so chunks land near the
  target, not on it. Use `--pages` when you need deterministic boundaries.
- Chunk sizes may exceed the target when a single page carries a huge image;
  a page is never split across files.
- Bookmarks/outlines and form fields are not carried into the parts — only
  pages and their resources. Document info metadata is copied.
- On a 3 GB file, `--size` does a full page-graph walk first; `--pages` and
  `--parts` skip that and start writing immediately.
