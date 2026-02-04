#!/usr/bin/env python3
"""Local photo-duplicate review UI.

Serves a tiny browser UI to compare likely-duplicate photos (e.g. "IMG1234.jpg"
and "IMG1234 (2).jpg") side-by-side. Click one to (optionally) delete/move it
to trash, then advance to the next pair.

No third-party dependencies.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import shutil
import struct
import threading
from dataclasses import dataclass
from datetime import datetime
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tif", ".tiff"}
_DUP_SUFFIX_RE = re.compile(r"^(.*?)( \([0-9]+\))$", re.IGNORECASE)


def get_image_dimensions(path: Path) -> tuple[int, int] | None:
    """Extract image dimensions without external dependencies.

    Returns (width, height) or None if unable to determine.
    """
    try:
        with path.open("rb") as f:
            head = f.read(24)
            if len(head) < 24:
                return None

            # PNG
            if head[:8] == b'\x89PNG\r\n\x1a\n':
                f.seek(16)
                w, h = struct.unpack('>II', f.read(8))
                return (w, h)

            # JPEG
            if head[:2] == b'\xff\xd8':
                f.seek(0)
                size = 2
                ftype = 0
                while not 0xc0 <= ftype <= 0xcf or ftype in (0xc4, 0xc8, 0xcc):
                    f.seek(size, 1)
                    byte = f.read(1)
                    while byte and byte != b'\xff':
                        byte = f.read(1)
                    if not byte:
                        return None
                    byte = f.read(1)
                    while byte == b'\xff':
                        byte = f.read(1)
                    if not byte:
                        return None
                    ftype = ord(byte)
                    size_bytes = f.read(2)
                    if len(size_bytes) < 2:
                        return None
                    size = struct.unpack('>H', size_bytes)[0] - 2
                if size < 5:
                    return None
                f.read(1)  # precision
                h, w = struct.unpack('>HH', f.read(4))
                return (w, h)

            # GIF
            if head[:6] in (b'GIF87a', b'GIF89a'):
                w, h = struct.unpack('<HH', head[6:10])
                return (w, h)

            # BMP
            if head[:2] == b'BM':
                w, h = struct.unpack('<ii', head[18:26])
                return (abs(w), abs(h))

            # WEBP
            if head[:4] == b'RIFF' and head[8:12] == b'WEBP':
                if head[12:16] == b'VP8 ':
                    f.seek(26)
                    data = f.read(4)
                    w = struct.unpack('<H', data[0:2])[0] & 0x3fff
                    h = struct.unpack('<H', data[2:4])[0] & 0x3fff
                    return (w, h)
                elif head[12:16] == b'VP8L':
                    f.seek(21)
                    data = f.read(4)
                    bits = struct.unpack('<I', data)[0]
                    w = (bits & 0x3fff) + 1
                    h = ((bits >> 14) & 0x3fff) + 1
                    return (w, h)
                elif head[12:16] == b'VP8X':
                    f.seek(24)
                    data = f.read(6)
                    w = struct.unpack('<I', data[0:3] + b'\x00')[0] + 1
                    h = struct.unpack('<I', data[3:6] + b'\x00')[0] + 1
                    return (w, h)

            # TIFF
            if head[:2] in (b'II', b'MM'):
                endian = '<' if head[:2] == b'II' else '>'
                f.seek(4)
                offset = struct.unpack(endian + 'I', f.read(4))[0]
                f.seek(offset)
                num_tags = struct.unpack(endian + 'H', f.read(2))[0]
                w = h = None
                for _ in range(num_tags):
                    tag_data = f.read(12)
                    if len(tag_data) < 12:
                        break
                    tag = struct.unpack(endian + 'H', tag_data[:2])[0]
                    value = struct.unpack(endian + 'I', tag_data[8:12])[0]
                    if tag == 256:  # ImageWidth
                        w = value
                    elif tag == 257:  # ImageLength
                        h = value
                    if w and h:
                        return (w, h)
    except Exception:
        pass
    return None


def normalize_key(p: Path) -> str:
    stem = p.stem
    m = _DUP_SUFFIX_RE.match(stem)
    if m:
        stem = m.group(1)
    return stem.lower()


def folder_rel_key(root: Path, p: Path) -> str:
    """Relative directory key for grouping.

    We only compare potential duplicates *within the same folder*, so the folder
    becomes part of the grouping key.
    """
    try:
        rel = p.parent.relative_to(root)
    except Exception:
        # Shouldn't happen for indexed files, but keep grouping deterministic.
        rel = p.parent
    rel_s = rel.as_posix()
    return rel_s or "."


def iso_mtime(ts: float) -> str:
    return datetime.fromtimestamp(ts).isoformat(timespec="seconds")


@dataclass
class FileInfo:
    id: int
    relpath: str
    name: str
    size_bytes: int
    mtime_iso: str
    width: int | None = None
    height: int | None = None


class DuplicateState:
    def __init__(self, root: Path, *, permanent_delete: bool, subfolder: str | None = None):
        self.root = root
        self.permanent_delete = permanent_delete
        self.trash_dir = root / ".image-dup-trash"
        self.subfolder = subfolder

        self._lock = threading.Lock()
        self._paths: dict[int, Path] = {}
        self._info: dict[int, FileInfo] = {}
        # Candidate pairs: (left_id, right_id, group_key). These are stable indices
        # for cursor-based paging.
        self._pairs: list[tuple[int, int, str]] = []

        # Backwards-compat single-pair navigation (kept, but not used by the new UI).
        # group_key is a string that includes the folder + normalized key.
        self._groups: list[tuple[str, list[int]]] = []  # (group_key, ids)
        self._group_idx = 0

    def list_subfolders(self) -> list[str]:
        """List immediate subfolders under root (e.g., year folders)."""
        subfolders = []
        try:
            for item in sorted(self.root.iterdir()):
                if item.is_dir() and not item.name.startswith(".") and item.name != ".image-dup-trash":
                    subfolders.append(item.name)
        except Exception:
            pass
        return subfolders

    def build_index(self) -> None:
        # os.walk is significantly faster than Path.rglob for large trees.
        # If subfolder is specified, only index that subfolder
        scan_root = self.root / self.subfolder if self.subfolder else self.root

        paths: list[Path] = []
        for dirpath, dirnames, filenames in os.walk(scan_root):
            # Prune hidden dirs + our trash folder.
            dirnames[:] = [
                d
                for d in dirnames
                if not d.startswith(".") and d != ".image-dup-trash"
            ]
            for fn in filenames:
                if fn.startswith("."):
                    continue
                p = Path(dirpath) / fn
                if p.suffix.lower() in IMAGE_EXTS:
                    paths.append(p)

        paths.sort(key=lambda p: p.name.lower())
        paths_by_id = {i: p for i, p in enumerate(paths)}

        info_by_id: dict[int, FileInfo] = {}
        for i, p in paths_by_id.items():
            st = p.stat()
            dims = get_image_dimensions(p)
            width, height = dims if dims else (None, None)
            info_by_id[i] = FileInfo(
                id=i,
                relpath=str(p.relative_to(self.root)),
                name=p.name,
                size_bytes=st.st_size,
                mtime_iso=iso_mtime(st.st_mtime),
                width=width,
                height=height,
            )

        # IMPORTANT: only compare duplicates within the same directory.
        # Key = (relative_folder, normalized_filename_key)
        groups: dict[tuple[str, str], list[int]] = {}
        for i, p in paths_by_id.items():
            groups.setdefault((folder_rel_key(self.root, p), normalize_key(p)), []).append(i)

        grouped: list[tuple[str, list[int]]] = []
        pairs: list[tuple[int, int, str]] = []
        for (folder_key, name_key), ids in groups.items():
            if len(ids) < 2:
                continue
            ids.sort(key=lambda fid: paths_by_id[fid].name.lower())
            group_key = name_key if folder_key == "." else f"{folder_key} / {name_key}"
            grouped.append((group_key, ids.copy()))

            # Pairing strategy: choose a "base" (prefer the non-suffixed filename)
            # then pair base vs every other candidate in the group.
            base = ids[0]
            for fid in ids:
                stem = paths_by_id[fid].stem
                if stem.lower() == name_key and _DUP_SUFFIX_RE.match(stem) is None:
                    base = fid
                    break

            # Get base aspect ratio for filtering
            base_info = info_by_id[base]
            base_aspect = None
            if base_info.width and base_info.height and base_info.height > 0:
                base_aspect = base_info.width / base_info.height

            for other in ids:
                if other == base:
                    continue

                # Only pair if aspect ratios match (within small tolerance for rounding)
                if base_aspect is not None:
                    other_info = info_by_id[other]
                    if other_info.width and other_info.height and other_info.height > 0:
                        other_aspect = other_info.width / other_info.height
                        # Allow 0.1% tolerance for rounding errors
                        if abs(base_aspect - other_aspect) / base_aspect > 0.001:
                            continue
                    else:
                        # Skip if we can't determine aspect ratio
                        continue

                pairs.append((base, other, group_key))

        grouped.sort(key=lambda kv: kv[0])
        pairs.sort(key=lambda t: (t[2], paths_by_id[t[0]].name.lower(), paths_by_id[t[1]].name.lower()))

        with self._lock:
            self._paths = paths_by_id
            self._info = info_by_id
            self._pairs = pairs
            self._groups = grouped
            self._group_idx = 0

    def _file_info_unlocked(self, fid: int) -> FileInfo:
        return self._info[fid]

    def pairs_page(self, *, cursor: int, limit: int) -> dict:
        """Return a page of valid pairs using a stable cursor.

        The cursor is an index into the precomputed candidate-pair list.
        Deleted/missing files are skipped.
        """
        limit = max(1, min(int(limit), 200))
        cursor = max(0, int(cursor))

        out = []
        with self._lock:
            i = cursor
            while i < len(self._pairs) and len(out) < limit:
                left_id, right_id, key = self._pairs[i]
                pair_id = i
                i += 1
                if left_id not in self._paths or right_id not in self._paths:
                    continue
                out.append(
                    {
                        "pair_id": pair_id,
                        "group_key": key,
                        "left": self._file_info_unlocked(left_id).__dict__,
                        "right": self._file_info_unlocked(right_id).__dict__,
                    }
                )

            done = i >= len(self._pairs)
            return {
                "pairs": out,
                "next_cursor": i,
                "done": done,
                "total_candidate_pairs": len(self._pairs),
            }

    def _advance_to_valid_group_unlocked(self) -> None:
        while self._group_idx < len(self._groups) and len(self._groups[self._group_idx][1]) < 2:
            self._group_idx += 1

    def current_pair(self) -> dict:
        with self._lock:
            self._advance_to_valid_group_unlocked()
            if self._group_idx >= len(self._groups):
                return {"done": True}

            key, ids = self._groups[self._group_idx]
            left, right = ids[0], ids[1]
            return {
                "done": False,
                "group_key": key,
                "group_index": self._group_idx + 1,
                "group_count": len(self._groups),
                "left": self._file_info_unlocked(left).__dict__,
                "right": self._file_info_unlocked(right).__dict__,
            }

    def skip_group(self) -> dict:
        with self._lock:
            self._group_idx += 1
        return self.current_pair()

    def delete_id(self, fid: int) -> dict:
        with self._lock:
            if fid not in self._paths:
                raise FileNotFoundError(f"Unknown id {fid}")
            p = self._paths[fid]

            # Remove from all groups that contain it (normally exactly one).
            for idx, (key, ids) in enumerate(self._groups):
                if fid in ids:
                    ids.remove(fid)
                    self._groups[idx] = (key, ids)

            # Perform filesystem action.
            if self.permanent_delete:
                p.unlink(missing_ok=False)
            else:
                rel = p.relative_to(self.root)
                dest = self.trash_dir / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                # Avoid clobbering an existing trashed file.
                if dest.exists():
                    base = dest.with_suffix("")
                    ext = dest.suffix
                    n = 2
                    while True:
                        cand = Path(str(base) + f" ({n})" + ext)
                        if not cand.exists():
                            dest = cand
                            break
                        n += 1
                self.trash_dir.mkdir(parents=True, exist_ok=True)
                try:
                    p.rename(dest)
                except OSError:
                    shutil.copy2(p, dest)
                    p.unlink()

            # Remove from our in-memory index so future pages skip it.
            self._paths.pop(fid, None)
            self._info.pop(fid, None)

        return {"ok": True, "deleted_id": fid}

    def open_path_for_id(self, fid: int) -> Path:
        with self._lock:
            return self._paths[fid]


def _read_json_body(handler: SimpleHTTPRequestHandler) -> dict:
    length = int(handler.headers.get("Content-Length", "0") or "0")
    raw = handler.rfile.read(length) if length > 0 else b"{}"
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception as e:
        raise ValueError(f"Invalid JSON body: {e}")


def _send_json(handler: SimpleHTTPRequestHandler, payload: dict, status: int = 200) -> None:
    data = json.dumps(payload).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


class Handler(SimpleHTTPRequestHandler):
    # Will be injected.
    state: DuplicateState

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)

        if parsed.path == "/api/current":
            return _send_json(self, self.state.current_pair())

        if parsed.path == "/api/pairs":
            qs = parse_qs(parsed.query or "")
            cursor = int((qs.get("cursor") or ["0"])[0])
            limit = int((qs.get("limit") or ["24"])[0])
            return _send_json(self, self.state.pairs_page(cursor=cursor, limit=limit))

        if parsed.path == "/api/subfolders":
            return _send_json(self, {"subfolders": self.state.list_subfolders(), "current": self.state.subfolder})

        if parsed.path.startswith("/img/"):
            try:
                fid = int(parsed.path.split("/", 2)[2])
                p = self.state.open_path_for_id(fid)
                ctype, _ = mimetypes.guess_type(p.name)
                ctype = ctype or "application/octet-stream"
                st = p.stat()
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(st.st_size))
                self.end_headers()
                with p.open("rb") as f:
                    shutil.copyfileobj(f, self.wfile)
            except Exception as e:
                return _send_json(self, {"error": str(e)}, status=404)
            return

        # Static UI
        return super().do_GET()

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/api/delete":
                body = _read_json_body(self)
                fid = int(body.get("id"))
                return _send_json(self, self.state.delete_id(fid))

            if parsed.path == "/api/skip":
                return _send_json(self, self.state.skip_group())

            if parsed.path == "/api/set-subfolder":
                body = _read_json_body(self)
                subfolder = body.get("subfolder")
                # Validate subfolder if provided
                if subfolder:
                    subfolder_path = self.state.root / subfolder
                    if not subfolder_path.exists() or not subfolder_path.is_dir():
                        return _send_json(self, {"error": "Invalid subfolder"}, status=400)
                self.state.subfolder = subfolder
                self.state.build_index()
                return _send_json(self, {"ok": True, "subfolder": subfolder})

            return _send_json(self, {"error": "Unknown endpoint"}, status=404)
        except Exception as e:
            return _send_json(self, {"error": str(e)}, status=400)


def main() -> int:
    ap = argparse.ArgumentParser(description="Side-by-side duplicate photo reviewer")
    ap.add_argument("--root", default="/home/jef/Pictures/photos", help="Root folder containing year subfolders")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument(
        "--permanent-delete",
        action="store_true",
        help="Delete files permanently (default is to move to --root/.image-dup-trash)",
    )
    args = ap.parse_args()

    root = Path(args.root).expanduser().resolve()
    if not root.exists() or not root.is_dir():
        raise SystemExit(f"Root does not exist or is not a directory: {root}")

    state = DuplicateState(root, permanent_delete=args.permanent_delete)
    print(f"Indexing images under: {root} (this may take a while)...")
    state.build_index()
    print("Index complete.")

    static_dir = (Path(__file__).parent / "static").resolve()
    handler_cls = type("BoundHandler", (Handler,), {"state": state})
    httpd = ThreadingHTTPServer((args.host, args.port), lambda *a, **kw: handler_cls(*a, directory=str(static_dir), **kw))
    url = f"http://{args.host}:{args.port}/"
    print(f"Open: {url}")
    print("Controls: click an image to delete/move-to-trash; right-arrow or 'n' to skip")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
