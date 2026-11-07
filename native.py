"""Read local Codex projects and rollouts; register imports using Codex itself."""

import csv
import hashlib
import io
import json
import os
import queue
import re
import shutil
import sqlite3
import subprocess
import threading
import time
from compression import zstd
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath

from storage import ID, SyncError, linklike, read_json, safe_path, write_json

UUID = r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}"
ROLLOUT = re.compile(
    rf"rollout-(\d{{4}})-(\d{{2}})-(\d{{2}})T\d{{2}}-\d{{2}}-\d{{2}}-({UUID})(?:_{UUID})?\.jsonl(?:\.zst)?\Z"
)
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
ORDINAL = re.compile(rb'^\{"timestamp":"[^"]+","ordinal":(\d+),')


@dataclass
class Project:
    id: str
    title: str
    roots: tuple
    native_id: str | None = None


@dataclass
class Chat:
    id: str
    title: str
    path: Path
    cwd: str
    archived: bool
    project: str | None
    updated: float


def canonical(path):
    return os.path.normcase(os.path.normpath(unextended(str(path))))


def unextended(path):
    if path.startswith("\\\\?\\UNC\\"):
        return "\\\\" + path[8:]
    return path.removeprefix("\\\\?\\")


def rollout_open(path):
    return zstd.open(path, "rb") if path.name.endswith(".zst") else path.open("rb")


def meta(path, ident):
    with rollout_open(path) as f:
        event = json.loads(f.readline(32 * 1024 * 1024))
    data = event.get("payload")
    if event.get("type") != "session_meta" or not isinstance(data, dict) or data.get("id") != ident:
        raise SyncError(f"Rollout identity did not match chat {ident}.")
    return data


def catalog(home):
    global_state = read_json(home / ".codex-global-state.json", {})
    if not isinstance(global_state, dict):
        raise SyncError("Could not read the Codex desktop project list.")
    legacy_map = {}
    for host, entries in global_state.get("app-server-project-id-by-legacy-project-id-by-host", {}).items():
        if isinstance(entries, dict) and (host == "local" or (
            host.startswith("local:") and canonical(host[6:]) == canonical(home)
        )):
            legacy_map.update(entries)
    projects = {}
    for ident, p in global_state.get("local-projects", {}).items():
        if ID.fullmatch(ident) and p.get("rootPaths") and not p.get("chatGptBacking"):
            projects[ident] = Project(ident, p.get("name") or ident, tuple(p["rootPaths"]), legacy_map.get(ident))
    rows = {}
    databases = sorted(home.glob("state_*.sqlite"), key=lambda p: int(p.stem[6:]) if p.stem[6:].isdigit() else -1)
    if databases:
        db = sqlite3.connect(databases[-1].resolve().as_uri() + "?mode=ro", uri=True, timeout=3)
        db.row_factory = sqlite3.Row
        try:
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "projects" in tables and "project_roots" in tables:
                for ident, title in db.execute("SELECT id,name FROM projects"):
                    roots = tuple(r[0] for r in db.execute(
                        "SELECT path FROM project_roots WHERE project_id=? ORDER BY position", (ident,)))
                    if roots and ID.fullmatch(ident):
                        equivalents = [p for p in projects.values() if p.native_id == ident
                                       or tuple(map(canonical, p.roots)) == tuple(map(canonical, roots))]
                        if equivalents:
                            for p in equivalents:
                                p.native_id = ident
                        else:
                            projects[ident] = Project(ident, title, roots, ident)
            if "threads" in tables:
                columns = {r[1] for r in db.execute("PRAGMA table_info(threads)")}
                names = [n for n in ("id", "rollout_path", "cwd", "archived", "title", "name",
                                      "project_id", "updated_at", "source", "preview") if n in columns]
                if "id" in names:
                    rows = {r["id"]: dict(r) for r in db.execute("SELECT " + ",".join(names) + " FROM threads")}
        finally:
            db.close()
    titles = {}
    index = home / "session_index.jsonl"
    if index.is_file():
        with index.open(encoding="utf-8") as f:
            for line in f:
                try:
                    entry = json.loads(line)
                    titles[entry["id"]] = entry.get("thread_name")
                except (ValueError, KeyError):
                    continue
    paths = {}
    for directory in ("sessions", "archived_sessions"):
        for path in (home / directory).rglob("rollout-*.jsonl*"):
            match = ROLLOUT.fullmatch(path.name)
            if match and not linklike(path) and path.is_file():
                paths.setdefault(match[4], []).append((path, directory == "archived_sessions"))
    assignments = global_state.get("thread-project-assignments", {})
    native_to_local = {p.native_id: p.id for p in projects.values() if p.native_id}
    chats = {}
    for ident, options in paths.items():
        row = rows.get(ident, {})
        indexed = canonical(row.get("rollout_path", ""))
        path, archived = max(options, key=lambda v: (
            canonical(v[0]) in (indexed, indexed + ".zst"), v[1] == bool(row.get("archived")),
            v[0].stat().st_mtime_ns, not v[0].name.endswith(".zst")
        ))
        try:
            data = {} if row else meta(path, ident)
            if "subagent" in str(row.get("source") or data.get("source", "")).lower():
                continue
            cwd = unextended(row.get("cwd") or data.get("cwd") or "")
            assignment = assignments.get(ident) or {}
            pid = assignment.get("projectId") if assignment.get("projectKind") == "local" else None
            pid = pid if pid in projects else native_to_local.get(row.get("project_id"))
            if pid is None and ident not in global_state.get("projectless-thread-ids", []):
                folder = canonical(cwd)
                matches = [(len(canonical(r)), p.id) for p in projects.values() for r in p.roots
                           if folder == canonical(r) or folder.startswith(canonical(r).rstrip(os.sep) + os.sep)]
                pid = max(matches)[1] if matches else None
            title = row.get("name") or titles.get(ident) or row.get("title") or row.get("preview") or ident
            chats[ident] = Chat(ident, str(title), path, cwd, archived, pid,
                                row.get("updated_at") or path.stat().st_mtime)
        except (OSError, ValueError, SyncError, zstd.ZstdError):
            continue
    return projects, chats


def executable():
    override = os.environ.get("CODEX_SYNC_CODEX")
    if override:
        return override
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", "")) / "OpenAI" / "Codex" / "bin"
        choices = sorted(base.glob("*/codex.exe"), key=lambda p: p.stat().st_mtime, reverse=True)
        if choices:
            return str(choices[0])
    else:
        for directory in (Path("/Applications"), Path.home() / "Applications"):
            for relative in ("Contents/Resources/codex", "Contents/Resources/bin/codex"):
                p = directory / "Codex.app" / relative
                if p.is_file():
                    return str(p)
    return shutil.which("codex")


def codex_open():
    if os.name == "nt":
        p = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True,
                           errors="replace", creationflags=NO_WINDOW, timeout=10, check=True)
        return any(row[0].lower() in {"codex.exe", "codex app.exe"} for row in csv.reader(io.StringIO(p.stdout)) if row)
    p = subprocess.run(["ps", "-ax", "-o", "comm="], capture_output=True, text=True,
                       timeout=10, check=True)
    return any(Path(line.strip()).name.lower() == "codex" or "/codex.app/" in line.lower()
               for line in p.stdout.splitlines())


class RpcError(SyncError):
    def __init__(self, method, error):
        self.code = error.get("code")
        super().__init__(f"Codex {method}: {error.get('message', 'request failed')}")


class AppServer:
    def __init__(self, home):
        command = executable()
        if not command:
            raise SyncError("Install Codex on this computer or set CODEX_SYNC_CODEX to its executable.")
        self.proc = subprocess.Popen([command, "app-server", "--listen", "stdio://"],
                                     env={**os.environ, "CODEX_HOME": str(home)}, cwd=home,
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, encoding="utf-8", errors="replace",
                                     creationflags=NO_WINDOW)
        self.messages = queue.Queue()
        self.serial = 0
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()
        try:
            self.call("initialize", {"clientInfo": {"name": "codex-drive-sync", "version": "1.0.0"},
                                     "capabilities": {"experimentalApi": True}})
            self.send({"method": "initialized"})
        except Exception:
            self.close()
            raise

    def _read(self):
        for line in self.proc.stdout:
            try:
                self.messages.put(json.loads(line))
            except ValueError:
                continue
        self.messages.put(None)

    def send(self, value):
        self.proc.stdin.write(json.dumps(value, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()

    def call(self, method, params):
        self.serial += 1
        self.send({"id": self.serial, "method": method, "params": params})
        deadline = time.monotonic() + 30
        while True:
            try:
                result = self.messages.get(timeout=max(0, deadline - time.monotonic()))
            except queue.Empty:
                raise SyncError(f"Codex did not answer {method} in time.") from None
            if result is None:
                raise SyncError(f"Codex stopped while handling {method}.")
            if result.get("id") == self.serial and ("result" in result or "error" in result):
                if "error" in result:
                    raise RpcError(method, result["error"])
                return result["result"]
            if "method" in result and "id" in result:
                self.send({"id": result["id"], "error": {"code": -32601, "message": "Unsupported request"}})

    def close(self):
        self.proc.stdin.close()
        try:
            self.proc.wait(timeout=6)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=5)
        self.reader.join(timeout=2)
        self.proc.stdout.close()


def source_suffix(path, prefix):
    """Return path components for a foreign path inside a source root."""
    if not isinstance(path, str) or not isinstance(prefix, str) or not prefix:
        return None
    path, prefix = unextended(path), unextended(prefix)
    if PureWindowsPath(prefix).drive:
        try:
            return PureWindowsPath(path).relative_to(PureWindowsPath(prefix)).parts
        except ValueError:
            return None
    try:
        return Path(path).relative_to(Path(prefix)).parts if os.name != "nt" else (
            () if path == prefix else tuple(path[len(prefix.rstrip("/")) + 1:].split("/"))
            if path.startswith(prefix.rstrip("/") + "/") else None)
    except ValueError:
        return None


def remap(source, target, ident, origin, destination):
    """Preserve event order, IDs, ordinals and text; translate only runtime paths."""
    meta(source, ident)
    current = str(destination)
    def translate(value):
        suffix = source_suffix(value, origin)
        try:
            return str(safe_path(destination, "/".join(suffix))) if suffix else current
        except SyncError:
            return current

    with rollout_open(source) as src, target.open("wb") as dst:
        for line in src:
            if not line.endswith(b"\n"):
                raise SyncError("The source chat ended mid-record; will retry.")
            # Most rollout records contain neither workspace field.
            if b'"session_meta"' in line or b'"turn_context"' in line:
                event = json.loads(line)
                if event.get("type") in {"session_meta", "turn_context"}:
                    data = event["payload"]
                    changed = False
                    for key in ("cwd", "runtime_workspace_roots", "workspace_roots", "writable_roots"):
                        if key not in data:
                            continue
                        value = data[key]
                        new = translate(value) if isinstance(value, str) else (
                            [translate(p) for p in value] if isinstance(value, list) and all(
                                isinstance(p, str) for p in value) else value)
                        if new != value:
                            data[key] = new
                            changed = True
                    if changed:
                        line = (json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
            dst.write(line)


def is_prefix(path, candidate):
    with rollout_open(path) as a, candidate.open("rb") as b:
        for chunk in iter(lambda: a.read(1024 * 1024), b""):
            if chunk != b.read(len(chunk)):
                return False
    return True


def settings_event(line):
    return b'"thread_settings_applied"' in line and (
        (record := json.loads(line)).get("type") == "event_msg"
        and record.get("payload", {}).get("type") == "thread_settings_applied"
    )


def logical_line(line):
    """Ordinals are local to a physical rollout; settings events are machine-local."""
    if b'"session_meta"' in line or b'"turn_context"' in line:
        event = json.loads(line)
        if event.get("type") in {"session_meta", "turn_context"}:
            event.pop("ordinal", None)
            for key in ("cwd", "runtime_workspace_roots", "workspace_roots", "writable_roots"):
                event.get("payload", {}).pop(key, None)
            return json.dumps(event, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"
    match = ORDINAL.match(line)
    return line[:match.start(1)] + b"0" + line[match.end(1):] if match else line


def history_hash(path):
    sha = hashlib.sha256()
    with rollout_open(path) as f:
        for line in f:
            if not line.endswith(b"\n"):
                raise SyncError("The source chat ended mid-record; will retry.")
            if not settings_event(line):
                sha.update(logical_line(line))
    return sha.hexdigest()


def append_portable(local, remote, target):
    """Keep local bytes and add only new history when resume settings differ."""
    with rollout_open(local) as existing, remote.open("rb") as incoming, target.open("wb") as output:
        right = (line for line in incoming if not settings_event(line))
        ordinal = None
        for line in existing:
            if not settings_event(line):
                other = next(right, None)
                if other is None or logical_line(line) != logical_line(other):
                    return False
            match = ORDINAL.match(line)
            if match:
                ordinal = int(match[1])
            output.write(line)
        for line in right:
            match = ORDINAL.match(line)
            if match and ordinal is not None:
                ordinal += 1
                line = line[:match.start(1)] + str(ordinal).encode() + line[match.end(1):]
            output.write(line)
    return True


def target_rollout(home, ident, name, archived):
    m = ROLLOUT.fullmatch(name or "")
    if not m or m[4] != ident:
        raise SyncError("Incoming chat filename did not match its ID.")
    root = home / ("archived_sessions" if archived else "sessions")
    if not archived:
        root = root.joinpath(*m.groups()[:3])
    return root / name.removesuffix(".zst")


def bridge_desktop(home, project, native_id, threads):
    """Bridge legacy Desktop sidebar layouts, only when that layout exists."""
    path = home / ".codex-global-state.json"
    info = read_json(path, {})
    if "local-projects" not in info:
        return
    items = info["local-projects"]
    updated = int(time.time() * 1000)
    items.setdefault(project.id, {"id": project.id, "name": project.title,
                                  "rootPaths": list(project.roots), "createdAt": updated, "updatedAt": updated})
    ordering = info.setdefault("project-order", [])
    if project.id not in ordering:
        ordering.append(project.id)
    if native_id:
        info.setdefault("app-server-project-id-by-legacy-project-id-by-host", {}).setdefault(
            f"local:{home}", {})[project.id] = native_id
    entries = info.setdefault("thread-project-assignments", {})
    for ident in threads:
        entries.setdefault(ident, {"projectKind": "local", "projectId": project.id})
    write_json(path, info)
