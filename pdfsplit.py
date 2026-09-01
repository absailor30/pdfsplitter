#!/usr/bin/env python3
"""Split very large PDFs (multi-GB) into smaller files.

Uses pikepdf (qpdf), which memory-maps the source and copies pages lazily,
so peak RAM stays roughly proportional to the chunk being written, not to
the size of the input file.

Modes (pick exactly one):
  --pages N     fixed number of pages per output file
  --parts K     split into K roughly equal parts (by page count)
  --size MB     grow each chunk until it is about MB megabytes
  --ranges S    explicit 1-based ranges, e.g. "1-100,101-250,900-"
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

import pikepdf

MB = 1024 * 1024


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f}{unit}"
        n /= 1024


def parse_ranges(spec: str, total: int) -> list[tuple[int, int]]:
    """Parse "1-100,150,200-" into 0-based [start, end) tuples."""
    out: list[tuple[int, int]] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, _, b = part.partition("-")
            start = int(a) if a.strip() else 1
            end = int(b) if b.strip() else total
        else:
            start = end = int(part)
        if start < 1 or end > total or start > end:
            raise ValueError(f"range {part!r} is outside 1-{total}")
        out.append((start - 1, end))
    if not out:
        raise ValueError("no ranges given")
    return out


def _page_bytes(page, seen: set[int]) -> int:
    """Approximate on-disk bytes a page adds, counting each shared object once.

    Walks the page's object graph and sums raw stream lengths. Shared objects
    (fonts, logos, XObjects reused across pages) are counted only for the first
    page in the chunk that pulls them in, which is what the written file does
    too. It is an estimate, not a promise: --size chunks land near the target,
    not exactly on it.
    """
    total = 0
    stack = [page.obj]
    local_seen: set[int] = set()
    while stack:
        obj = stack.pop()
        try:
            objgen = obj.objgen
        except AttributeError:
            objgen = None
        if objgen and objgen != (0, 0):
            key = objgen[0]
            if key in local_seen:
                continue
            local_seen.add(key)
            if key in seen:
                continue
            seen.add(key)
        try:
            if isinstance(obj, pikepdf.Stream):
                total += int(obj.get("/Length", 0) or 0)
        except Exception:
            pass
        try:
            if isinstance(obj, pikepdf.Dictionary) or isinstance(obj, pikepdf.Stream):
                total += 64
                for k, v in obj.items():
                    if k == "/Parent":
                        continue
                    stack.append(v)
            elif isinstance(obj, pikepdf.Array):
                stack.extend(list(obj))
        except Exception:
            pass
    return total


def size_chunks(pdf: pikepdf.Pdf, target: int) -> list[tuple[int, int]]:
    chunks: list[tuple[int, int]] = []
    start = 0
    used = 0
    seen: set[int] = set()
    for i, page in enumerate(pdf.pages):
        cost = _page_bytes(page, seen)
        if used and used + cost > target:
            chunks.append((start, i))
            start, used, seen = i, 0, set()
            cost = _page_bytes(page, seen)
        used += cost
    chunks.append((start, len(pdf.pages)))
    return chunks


def even_chunks(total: int, parts: int) -> list[tuple[int, int]]:
    if parts > total:
        parts = total
    base, extra = divmod(total, parts)
    chunks, start = [], 0
    for i in range(parts):
        end = start + base + (1 if i < extra else 0)
        chunks.append((start, end))
        start = end
    return chunks


def fixed_chunks(total: int, per: int) -> list[tuple[int, int]]:
    return [(s, min(s + per, total)) for s in range(0, total, per)]


def write_chunk(
    src: pikepdf.Pdf,
    start: int,
    end: int,
    out_path: Path,
    *,
    linearize: bool = False,
) -> int:
    with pikepdf.Pdf.new() as dst:
        dst.pages.extend(src.pages[start:end])
        try:
            with src.open_metadata() as meta_src, dst.open_metadata() as meta_dst:
                meta_dst.load_from_docinfo(src.docinfo)
                del meta_src
        except Exception:
            pass
        dst.save(
            out_path,
            linearize=linearize,
            compress_streams=True,
            object_stream_mode=pikepdf.ObjectStreamMode.generate,
        )
    return out_path.stat().st_size


def split(
    src_path: Path,
    out_dir: Path,
    *,
    pages: int | None = None,
    parts: int | None = None,
    size_mb: float | None = None,
    ranges: str | None = None,
    password: str = "",
    prefix: str | None = None,
    linearize: bool = False,
    dry_run: bool = False,
    quiet: bool = False,
) -> list[Path]:
    if not src_path.is_file():
        raise SystemExit(f"not a file: {src_path}")

    log = (lambda *a: None) if quiet else (lambda *a: print(*a, flush=True))
    t0 = time.time()
    log(f"opening {src_path} ({human(src_path.stat().st_size)})")

    with pikepdf.open(src_path, password=password) as pdf:
        total = len(pdf.pages)
        log(f"{total} pages")

        if ranges:
            chunks = parse_ranges(ranges, total)
        elif pages:
            chunks = fixed_chunks(total, pages)
        elif parts:
            chunks = even_chunks(total, parts)
        else:
            log("measuring pages for size-based split...")
            chunks = size_chunks(pdf, int(size_mb * MB))

        stem = prefix or src_path.stem
        width = max(3, len(str(len(chunks))))
        out_dir.mkdir(parents=True, exist_ok=True)

        written: list[Path] = []
        for i, (start, end) in enumerate(chunks, 1):
            name = f"{stem}_part{i:0{width}d}_p{start + 1}-{end}.pdf"
            out = out_dir / name
            if dry_run:
                log(f"  [dry-run] {name}  ({end - start} pages)")
                written.append(out)
                continue
            n = write_chunk(pdf, start, end, out, linearize=linearize)
            log(f"  {name}  {end - start} pages  {human(n)}")
            written.append(out)

    log(f"done: {len(written)} files in {time.time() - t0:.1f}s -> {out_dir}")
    return written


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="pdfsplit",
        description="Split a large PDF into smaller PDFs.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  pdfsplit big.pdf --pages 500\n"
            "  pdfsplit big.pdf --size 100 -o out/\n"
            "  pdfsplit big.pdf --parts 8\n"
            "  pdfsplit big.pdf --ranges '1-100,101-,'\n"
        ),
    )
    p.add_argument("input", type=Path)
    p.add_argument("-o", "--out-dir", type=Path, default=None,
                   help="output directory (default: <input>_split next to input)")
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--pages", type=int, help="pages per output file")
    mode.add_argument("--parts", type=int, help="number of roughly equal parts")
    mode.add_argument("--size", type=float, metavar="MB",
                      help="approximate megabytes per output file")
    mode.add_argument("--ranges", help="explicit page ranges, e.g. '1-100,101-200'")
    p.add_argument("--password", default="", help="password for an encrypted PDF")
    p.add_argument("--prefix", help="output filename prefix (default: input stem)")
    p.add_argument("--linearize", action="store_true",
                   help="linearize output for fast web view (slower, larger)")
    p.add_argument("--dry-run", action="store_true", help="list the split plan only")
    p.add_argument("-q", "--quiet", action="store_true")
    a = p.parse_args(argv)

    if a.pages is not None and a.pages < 1:
        p.error("--pages must be >= 1")
    if a.parts is not None and a.parts < 1:
        p.error("--parts must be >= 1")
    if a.size is not None and a.size <= 0:
        p.error("--size must be > 0")

    out_dir = a.out_dir or a.input.with_name(a.input.stem + "_split")
    try:
        split(
            a.input, out_dir,
            pages=a.pages, parts=a.parts, size_mb=a.size, ranges=a.ranges,
            password=a.password, prefix=a.prefix, linearize=a.linearize,
            dry_run=a.dry_run, quiet=a.quiet,
        )
    except pikepdf.PasswordError:
        print("error: PDF is encrypted; pass --password", file=sys.stderr)
        return 2
    except (ValueError, pikepdf.PdfError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
