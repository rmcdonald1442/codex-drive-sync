"""Portable, append-only exchange format for a Drive for desktop folder."""

import hashlib
import json
import os
import re
import stat
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

BLOCK = 1024 * 1024
ID = re.compile(r"[a-zA-Z0-9_-]{1,80}\Z")
SHA = re.compile(r"[0-9a-f]{64}\Z")
REV = re.compile(r"[0-9]{20}-[0-9a-f]{32}\Z")
MARKER = "codex-drive-sync.json"


class SyncError(Exception):
    pass


def checked(value, pattern=ID):
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise SyncError("Invalid identifier in sync data.")
    return value


def json_bytes(obj):
    return (json.dumps(obj, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n").encode()


def read_json(path, default=None):
    try:
        return json.loads(path.read_bytes())
    except FileNotFoundError:
        return default


@contextmanager
def temporary(parent):
    parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".codex-sync-", suffix=".tmp", dir=parent)
    os.close(fd)
    path = Path(name)
    try:
        yield path
    finally:
        path.unlink(missing_ok=True)


def write_json(path, obj):
    with temporary(path.parent) as tmp:
        tmp.write_bytes(json_bytes(obj))
        os.replace(tmp, path)


def linklike(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        and getattr(info, "st_reparse_tag", 0) == getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", -1)
    )


def safe_path(root, relative):
    """Use the same conservative file names on both OSes; reject links and junctions."""
    if not isinstance(relative, str) or "\\" in relative:
        raise SyncError("Invalid workspace path.")
    path = PurePosixPath(relative)
    if not path.parts or path.as_posix() != relative or path.is_absolute():
        raise SyncError(f"Invalid workspace path: {relative}")
    out = root
    if linklike(out):
        raise SyncError(f"Workspace is a link: {out}")
    for part in path.parts:
        if (
            part in {".", ".."} or part[-1:] in {" ", "."}
            or any(ch in '<>:"|?*' or ord(ch) < 32 for ch in part)
            or re.fullmatch(r"(CON|PRN|AUX|NUL|COM[0-9¹²³]|LPT[0-9¹²³])", part.split(".")[0], re.I)
        ):
            raise SyncError(f"Cannot sync this filename across both platforms: {relative}")
        out = out / part
        if linklike(out):
            raise SyncError(f"Workspace path is a link: {relative}")
    return out


def stamp(path):
    st = path.stat()
    if not stat.S_ISREG(st.st_mode):
        raise SyncError(f"Not a regular file: {path}")
    return [st.st_size, st.st_mtime_ns, st.st_ctime_ns, st.st_ino]


def hash_file(path):
    with path.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def copy_checked(source, target, expected=None, before=None):
    """Write to a sibling first. Never expose an incomplete or unverified file."""
    before = before if before is not None else stamp(source)
    with temporary(target.parent) as tmp:
        sha = hashlib.sha256()
        with source.open("rb") as src, tmp.open("wb") as dst:
            for chunk in iter(lambda: src.read(BLOCK), b""):
                sha.update(chunk)
                dst.write(chunk)
        actual = sha.hexdigest()
        if stamp(source) != before:
            raise SyncError(f"Changed while reading {source.name}; will retry.")
        if expected is not None and actual != expected:
            raise SyncError(f"Drive file failed its checksum: {source.name}")
        os.replace(tmp, target)
    return actual


class Exchange:
    def __init__(self, folder, create=False):
        self.folder = Path(folder).resolve()
        if not self.folder.is_dir():
            raise SyncError("The sync folder is unavailable. Check Google Drive for desktop.")
        marker = self.folder / MARKER
        info = read_json(marker)
        if info is None and create:
            # The ID distinguishes this folder's selections and local baselines.
            info = {"format": 1, "id": uuid.uuid4().hex}
            write_json(marker, info)
        if info is None:
            raise SyncError("No shared setup here yet. Select items and click Apply to create it.")
        if not isinstance(info, dict) or info.get("format") != 1:
            raise SyncError("This folder uses an unsupported sync format.")
        self.id = checked(info.get("id"))
        self.root = safe_path(self.folder, "codex-sync-data")

    def selections(self):
        result = {}
        for p in sorted((self.root / "selections").glob("*.json")):
            if not REV.fullmatch(p.stem):
                continue
            patch = read_json(p)
            if not isinstance(patch, dict):
                raise SyncError("A shared selection is unreadable; wait for Drive to finish.")
            for key, value in patch.items():
                checked(key)
                if value is None:
                    result.pop(key, None)
                elif isinstance(value, dict) and value.get("kind") in {"chat", "project"}:
                    result[key] = value
                else:
                    raise SyncError("A shared selection has an unsupported format.")
        return result

    def update_selections(self, edits):
        if edits:
            latest = max((p.stem for p in (self.root / "selections").glob("*.json")
                          if REV.fullmatch(p.stem)), default="")
            clock = max(time.time_ns(), int(latest[:20]) + 1 if latest else 0)
            write_json(self.root / "selections" / f"{clock:020d}-{uuid.uuid4().hex}.json", edits)

    def object_path(self, sha):
        sha = checked(sha, SHA)
        return safe_path(self.root, f"objects/{sha[:2]}/{sha}")

    def push(self, path, sha, original_stamp):
        obj = self.object_path(sha)
        if not obj.is_file():
            copy_checked(path, obj, sha, original_stamp)

    def pull(self, sha, dest):
        obj = self.object_path(sha)
        if not obj.is_file():
            raise SyncError("A Drive file has not arrived yet; will retry.")
        copy_checked(obj, dest, sha)

    def history(self, key):
        versions = {}
        edges = {}
        parents = set()
        directory = safe_path(self.root, f"versions/{checked(key)}")
        for path in directory.glob("*.json"):
            if not REV.fullmatch(path.stem):
                continue
            v = read_json(path)
            if not isinstance(v, dict) or not isinstance(v.get("data"), dict):
                raise SyncError("A sync version is unreadable; wait for Drive to finish.")
            previous = v.get("parents", [])
            if not isinstance(previous, list):
                raise SyncError("A sync version has an invalid parent list.")
            for parent in previous:
                parents.add(checked(parent, REV))
            edges[path.stem] = previous
            versions[path.stem] = v["data"]
        return versions, sorted(versions.keys() - parents), edges

    def publish(self, key, parents, data):
        rev = f"{time.time_ns():020d}-{uuid.uuid4().hex}"
        write_json(safe_path(self.root, f"versions/{checked(key)}/{rev}.json"),
                   {"parents": parents, "data": data})
        return rev
