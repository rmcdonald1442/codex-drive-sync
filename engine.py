"""Selection, change detection, and two-way transfer."""

import hashlib
import os
import re
import subprocess
from compression import zstd
from pathlib import Path

from native import (AppServer, NO_WINDOW, Project, RpcError, append_portable, bridge_desktop, catalog, codex_open,
                    history_hash, is_prefix, remap, source_suffix, target_rollout)
from storage import (Exchange, ID, MARKER, SHA, SyncError, checked, copy_checked, hash_file, linklike,
                     read_json, safe_path, stamp, temporary, write_json)

INTERVAL = 300
SKIP = {".git", ".hg", ".svn", ".codex", ".ssh", ".aws", ".venv", "venv", "node_modules",
        "__pycache__", ".pytest_cache", ".mypy_cache", ".cache", ".next", ".terraform",
        "dist", "build", "target", ".ds_store", "thumbs.db", ".npmrc", ".pypirc", ".netrc",
        "auth.json", "credentials.json", "secrets.json", "id_rsa", "id_ed25519"}
FILE_LIMIT = 100_000


def allowed(relative):
    return all(p.lower() not in SKIP and not p.lower().startswith((".env", ".codex-sync-"))
               and not p.lower().endswith((".pem", ".key", ".p12", ".pfx")) for p in relative.split("/"))


def slug(title):
    return re.sub(r"[^a-zA-Z0-9]+", "-", title).strip("-")[:42] or "Project"


def project_info(project):
    return {"title": project.title, "roots": len(project.roots)}


def project_desc(value):
    title = value.get("title")
    count = value.get("roots")
    if not isinstance(title, str) or not title or len(title) > 1000 or type(count) is not int or not 1 <= count <= 50:
        raise SyncError("Invalid project details in Drive.")
    return title, count


def fingerprint(path, known=None):
    s = stamp(path)
    if known and s == known[0]:
        return known
    h = hash_file(path)
    if stamp(path) != s:
        raise SyncError(f"Changed while reading {path.name}; will retry.")
    return [s, h]


def chat_data(chat, projects, cached):
    file = fingerprint(chat.path, cached.get("file"))
    if len(file) < 3:
        file.append(history_hash(chat.path))
        if stamp(chat.path) != file[0]:
            raise SyncError("Chat changed while reading; will retry.")
    value = {"sha": file[1], "file": chat.path.name, "archived": chat.archived,
             "title": chat.title, "cwd": chat.cwd, "project": chat.project}
    project = projects.get(chat.project)
    if project:
        matches = [(len(r), i, r) for i, r in enumerate(project.roots) if source_suffix(chat.cwd, r) is not None]
        _, i, root = max(matches, default=(0, 0, chat.cwd))
        value.update({"project_info": project_info(project), "root_index": i, "origin": root})
    else:
        value["origin"] = chat.cwd
    return file, value


def chat_signature(value, file):
    return {**{k: v for k, v in value.items() if k not in {"file", "sha"}}, "history": file[2]}


def version(exchange, key, local):
    versions, heads, _ = exchange.history(key)
    base = local.get("revision")
    if base and base not in versions:
        raise SyncError("Drive is catching up with a previously seen version; will retry.")
    if len(heads) > 1 and any(versions[h] != versions[heads[0]] for h in heads[1:]):
        raise SyncError("Two computers made conflicting edits. Both versions are preserved in Drive.")
    return versions, heads, versions[heads[0]] if heads else None


def workspace_versions(exchange, key, state):
    versions, heads, edges = exchange.history(key)
    revision = state.get("revision")
    if revision and revision not in versions:
        raise SyncError("Drive is catching up with a previously seen workspace; will retry.")
    if not heads:
        return versions, heads, {}, False
    if any(p not in versions for parents in edges.values() for p in parents):
        raise SyncError("Drive is still downloading workspace versions; will retry.")
    if len(heads) == 1:
        return versions, heads, versions[heads[0]].get("files", {}), False

    def ancestors(head):
        seen, pending = set(), [head]
        while pending:
            current = pending.pop()
            if current not in seen:
                seen.add(current)
                pending.extend(edges[current])
        return seen

    common = set.intersection(*(ancestors(head) for head in heads))
    common_parents = {p for h in common for p in edges[h]}
    bases = common - common_parents
    if len(bases) > 1:
        raise SyncError("These workspace versions have multiple common bases; both are preserved in Drive.")
    baseline = versions[next(iter(bases))].get("files", {}) if bases else {}
    merged, changed = dict(baseline), {}
    for head in heads:
        files = versions[head].get("files", {})
        if not isinstance(files, dict):
            raise SyncError("Invalid workspace inventory.")
        for path, sha in files.items():
            if sha == baseline.get(path):
                continue
            if path in changed and changed[path] != sha:
                raise SyncError(f"Conflicting Drive versions for {path}. Both versions are preserved.")
            changed[path] = sha
    merged.update(changed)
    return versions, heads, merged, True


def files_in(root):
    if linklike(root):
        raise SyncError(f"Workspace is a link: {root}")
    if not root.is_dir():
        return
    if (root / ".git").exists():
        try:
            git = subprocess.run(["git", "-C", str(root), "ls-files", "--cached", "--others",
                                  "--exclude-standard", "-z"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                 timeout=20, creationflags=NO_WINDOW, check=True)
        except (OSError, subprocess.SubprocessError) as exc:
            raise SyncError(f"Install Git or fix the repository to sync {root.name}.") from exc
        for name in sorted(set(git.stdout.decode("utf-8").split("\0")) - {""}):
            if not allowed(name):
                continue
            try:
                p = safe_path(root, name)
            except SyncError:
                continue
            if p.is_file():
                yield name, p
        return
    for base, dirs, names in os.walk(root, followlinks=False, onerror=lambda error: (_ for _ in ()).throw(error)):
        dirs[:] = sorted(n for n in dirs if allowed(n) and not linklike(Path(base) / n))
        for name in sorted(names):
            p = Path(base) / name
            rel = p.relative_to(root).as_posix()
            if not allowed(rel) or linklike(p):
                continue
            try:
                safe_path(root, rel)
            except SyncError:
                continue
            if p.is_file():
                yield rel, p


class Engine:
    def __init__(self, home, local_dir, workspace_base):
        self.home = home.resolve()
        self.local_dir = local_dir.resolve()
        self.workspace_base = workspace_base.resolve()

    def inspect(self, folder=None):
        projects, chats = catalog(self.home)
        setup = bool(folder and (folder / MARKER).is_file())
        if not setup:
            return projects, chats, {}, False
        exchange = Exchange(folder)
        selections = exchange.selections()
        mapping = read_json(self.local_dir / f"{exchange.id}.json", {}).get("project_map", {})
        for shared, entry in mapping.items():
            p = projects.get(entry.get("local")) or projects.get(entry.get("native"))
            if p and p.id != shared and shared not in projects:
                projects[shared] = Project(shared, p.title, p.roots, p.native_id)
                del projects[p.id]
                for chat in chats.values():
                    if chat.project == p.id:
                        chat.project = shared
        return projects, chats, selections, setup

    def run(self, folder, edits=None, report=lambda message: None):
        exchange = Exchange(folder, create=bool(edits))
        state_path = self.local_dir / f"{exchange.id}.json"
        state = read_json(state_path, {"chats": {}, "workspaces": {}, "project_map": {}})
        if edits:
            exchange.update_selections(edits)
        selected = exchange.selections()
        projects, chats = catalog(self.home)
        report(f"Checking {len(selected)} selections…")
        stats = {"sent": 0, "received": 0, "waiting": 0, "issues": []}

        def save():
            write_json(state_path, state)

        def note(name, exc):
            stats["issues"].append(f"{name}: {exc}")

        def project_for(key, desc):
            title, count = project_desc(desc)
            mapping = state["project_map"].get(key, {})
            existing = projects.get(mapping.get("local")) or projects.get(mapping.get("native")) or projects.get(key)
            if existing:
                if len(existing.roots) != count:
                    raise SyncError(f"{title} has a different number of workspace folders on this computer.")
                return existing
            folder = self.workspace_base / f"{slug(title)}-{checked(key)[-16:]}"
            roots = (folder,) if count == 1 else tuple(folder / f"Root-{i + 1}" for i in range(count))
            return Project(key, title, tuple(map(str, roots)), mapping.get("native"))

        selected_projects = {}
        local_to_shared = {}
        pending_projects = {}
        for key, desc in selected.items():
            if desc["kind"] != "project":
                continue
            try:
                p = project_for(key, desc)
                selected_projects[key] = p
                local_to_shared[p.id] = key
                if p.id not in projects:
                    pending_projects[key] = p
                if desc.get("files") is True:
                    self.sync_files(exchange, key, p, state["workspaces"].setdefault(key, {}), stats)
            except (OSError, ValueError, SyncError, subprocess.SubprocessError) as exc:
                note(desc.get("title", key), exc)

        all_ids = {k for k, v in selected.items() if v["kind"] == "chat"}
        all_ids.update(c.id for c in chats.values() if c.project in local_to_shared)
        # The sender's project membership arrives as separately indexed chat versions.
        for key in selected_projects:
            path = exchange.root / "members" / key
            all_ids.update(p.stem for p in path.glob("*.json") if ID.fullmatch(p.stem))
        incoming = []
        for ident in sorted(all_ids):
            chat = chats.get(ident)
            explicit = selected.get(ident, {}).get("kind") == "chat"
            try:
                key = "c-" + hashlib.sha256(ident.encode()).hexdigest()
                record = state["chats"].setdefault(ident, {})
                versions, heads, remote = version(exchange, key, record)
                base = record.get("revision")
                local_project = local_to_shared.get(chat.project, chat.project) if chat else None
                if not explicit and (
                    (chat and not record.get("pending") and local_project not in selected_projects)
                    or ((not chat or record.get("pending")) and (
                        not remote or remote.get("project") not in selected_projects))
                ):
                    continue
                if record.get("pending"):
                    pending = record["pending"]
                    if pending not in versions:
                        raise SyncError("Waiting for the incoming chat version.")
                    incoming.append((ident, chat, record, pending, versions[pending], True))
                    continue
                local = None
                if chat:
                    file, local = chat_data(chat, projects, record)
                    local["project"] = local_project
                    record["file"] = file
                if chat and local_project in selected_projects:
                    membership = safe_path(exchange.root, f"members/{local_project}/{ident}.json")
                    if not membership.exists():
                        write_json(membership, {})
                if local and local == remote:
                    record.update(revision=heads[0], signature=chat_signature(local, file))
                    continue
                changed = local is not None and chat_signature(local, file) != record.get("signature")
                remote_changed = bool(remote and base not in heads)
                if remote_changed and changed:
                    raise SyncError("This chat changed on both computers. Your local chat and Drive copy are preserved.")
                if remote_changed or (remote and not local and not base):
                    incoming.append((ident, chat, record, heads[0], remote, False))
                elif changed:
                    exchange.push(chat.path, file[1], file[0])
                    rev = exchange.publish(key, heads, local)
                    record.update(revision=rev, signature=chat_signature(local, file))
                    stats["sent"] += 1
            except (OSError, ValueError, SyncError, zstd.ZstdError) as exc:
                note(chat.title if chat else ident, exc)
        save()

        if incoming or pending_projects:
            if codex_open():
                stats["waiting"] = len(incoming) + len(pending_projects)
            else:
                self.home.mkdir(parents=True, exist_ok=True)
                server = AppServer(self.home)
                try:
                    def register(key, project, threads=()):
                        mapping = state["project_map"].setdefault(key, {})
                        native_id = mapping.get("native") or project.native_id
                        for root in project.roots:
                            Path(root).mkdir(parents=True, exist_ok=True)
                        if not native_id:
                            try:
                                reply = server.call("project/import", {
                                    "idempotencyKey": f"drive-sync:{exchange.id}:{key}", "name": project.title,
                                    "roots": [{"path": r} for r in project.roots], "threads": list(threads)})
                                native_id = reply["project"]["id"]
                            except RpcError as exc:
                                if exc.code != -32601 or "local-projects" not in read_json(
                                    self.home / ".codex-global-state.json", {}
                                ):
                                    raise
                        elif threads:
                            for tid in threads:
                                server.call("thread/metadata/update", {"threadId": tid, "projectId": native_id})
                        bridge_desktop(self.home, project, native_id, threads)
                        mapping.update(local=project.id, native=native_id)
                        projects[project.id] = project
                        save()

                    for key, p in pending_projects.items():
                        try:
                            register(key, p)
                        except (OSError, ValueError, SyncError) as exc:
                            note(p.title, exc)
                    for ident, chat, record, rev, data, resume in incoming:
                        try:
                            pid = data.get("project")
                            p = project_for(checked(pid), data["project_info"]) if pid else None
                            if p:
                                index = data.get("root_index")
                                if type(index) is not int or not 0 <= index < len(p.roots):
                                    raise SyncError("Invalid incoming workspace index.")
                                root = Path(p.roots[index])
                            else:
                                root = self.workspace_base / "Loose Chats" / ident
                            suffix = source_suffix(data.get("cwd"), data.get("origin"))
                            cwd = safe_path(root, "/".join(suffix)) if suffix else root
                            cwd.mkdir(parents=True, exist_ok=True)
                            self.install_chat(exchange, ident, chat, data, record, rev, root, save, resume)
                            server.call("thread/read", {"threadId": ident})
                            if chat and chat.archived:
                                server.call("thread/unarchive", {"threadId": ident})
                            try:
                                if data["title"] != ident:
                                    server.call("thread/name/set", {"threadId": ident, "name": data["title"]})
                                if p:
                                    register(pid, p, (ident,))
                            finally:
                                if data["archived"]:
                                    server.call("thread/archive", {"threadId": ident})
                            refreshed_projects, refreshed = catalog(self.home)
                            if ident not in refreshed:
                                raise SyncError("Codex did not index the imported chat.")
                            file, value = chat_data(refreshed[ident], refreshed_projects, {})
                            if pid:
                                value["project"] = pid
                            record.update(revision=rev, file=file, signature=chat_signature(value, file))
                            record.pop("pending", None)
                            save()
                            stats["received"] += 1
                        except (KeyError, OSError, ValueError, SyncError, zstd.ZstdError) as exc:
                            note(str(data.get("title", ident)), exc)
                finally:
                    server.close()
        save()
        return stats

    def install_chat(self, exchange, ident, chat, data, record, rev, root, save, resume):
        if not isinstance(data.get("title"), str) or type(data.get("archived")) is not bool:
            raise SyncError("Invalid incoming chat details.")
        # Codex can only name and assign active rollouts. Register new chats first,
        # then let Codex archive the ones that were archived at the source.
        original = target_rollout(self.home, ident, data.get("file"), False)
        dest = chat.path if chat else original
        if resume and chat:
            pending_file = record.get("pending_file")
            if pending_file != fingerprint(chat.path)[1]:
                raise SyncError("The staged chat changed locally before it could be registered.")
            return
        if linklike(dest):
            raise SyncError("The local chat is stored through a link.")
        if dest.name.endswith(".zst"):
            dest = dest.with_name(dest.name.removesuffix(".zst"))
        with temporary(self.local_dir) as downloaded, temporary(dest.parent) as staged:
            blob = downloaded.with_suffix(".zst") if data["file"].endswith(".zst") else downloaded
            try:
                exchange.pull(data["sha"], blob)
                remap(blob, staged, ident, data.get("origin"), root)
            finally:
                if blob != downloaded:
                    blob.unlink(missing_ok=True)
            if chat:
                if stamp(chat.path) != record["file"][0]:
                    raise SyncError("The local chat changed during sync; will retry.")
                if not is_prefix(chat.path, staged):
                    with temporary(dest.parent) as combined:
                        if not append_portable(chat.path, staged, combined):
                            raise SyncError("Incoming history is not an extension of this chat. Both copies were kept.")
                        os.replace(combined, staged)
                if chat.path != dest:
                    if dest.exists():
                        raise SyncError("Both compressed and plain copies exist locally; kept them both.")
                    sha = record["file"][1]
                    backup = self.local_dir / "compressed-backups" / ident / f"{sha}.jsonl.zst"
                    if not backup.exists():
                        copy_checked(chat.path, backup, sha, record["file"][0])
            elif dest.exists():
                raise SyncError("A local file already exists for this chat; refresh and retry.")
            os.replace(staged, dest)
            if chat and chat.path != dest:
                chat.path.unlink()
            record.update(pending=rev, pending_file=hash_file(dest))
            save()

    def sync_files(self, exchange, ident, project, state, stats):
        if any(linklike(Path(r)) for r in project.roots):
            raise SyncError("A project workspace is a link; choose a regular folder.")
        roots = tuple(Path(r).resolve() for r in project.roots)
        for root in roots:
            if root == Path(root.anchor) or root == Path.home() or (
                exchange.folder.is_relative_to(root) or root.is_relative_to(exchange.folder)
            ) or self.home.is_relative_to(root) or root.is_relative_to(self.home):
                raise SyncError("Choose a workspace outside the Codex data and Drive sync folders.")
        key = "w-" + hashlib.sha256(ident.encode()).hexdigest()
        versions, heads, remote, join_heads = workspace_versions(exchange, key, state)
        base = versions.get(state.get("revision"), {}).get("files", {})
        if not isinstance(remote, dict) or len(remote) > FILE_LIMIT:
            raise SyncError("Invalid workspace inventory.")
        cache, local, destinations = {}, {}, {}
        for i, root in enumerate(roots):
            if not root.is_dir() and root.exists():
                raise SyncError(f"Workspace path is not a folder: {root}")
            for name, path in files_in(root):
                rel = f"{i}/{name}"
                if len(local) >= FILE_LIMIT:
                    raise SyncError("Workspace exceeds the 100,000 file limit.")
                cache[rel] = fingerprint(path, state.get("cache", {}).get(rel))
                local[rel] = cache[rel][1]
                destinations[rel] = path
        merged = dict(remote)
        downloads = {}
        conflicts = []
        seen = set()
        for rel in sorted(set(local) | set(remote)):
            index, _, path = rel.partition("/")
            folded = rel.casefold()
            if folded in seen:
                raise SyncError(f"Two files use the same name on Windows: {rel}")
            seen.add(folded)
            if not index.isascii() or not index.isdecimal() or len(index) > 2 or int(index) >= len(roots) or not allowed(path):
                raise SyncError("Invalid or excluded file in the Drive workspace.")
            dst = safe_path(roots[int(index)], path)
            here, there, before = local.get(rel), remote.get(rel), base.get(rel)
            if there:
                checked(there, SHA)
            if here == there:
                continue
            if here is not None and there is not None and here != before and there != before:
                conflicts.append(path)
            elif here is not None and (there is None or there == before):
                merged[rel] = here
            elif there and (here is not None or before != there or not state.get("revision")):
                downloads[rel] = (dst, there)
        if conflicts:
            raise SyncError("Files changed on both computers: " + ", ".join(conflicts[:5]))
        for rel, sha in merged.items():
            if remote.get(rel) != sha:
                exchange.push(destinations[rel], sha, cache[rel][0])
        for rel, (dst, sha) in downloads.items():
            with temporary(dst.parent) as tmp:
                exchange.pull(sha, tmp)
                if (dst.exists() and rel not in cache) or (rel in cache and stamp(dst) != cache[rel][0]):
                    raise SyncError(f"{dst.name} changed during sync; will retry.")
                os.replace(tmp, dst)
            cache[rel] = [stamp(dst), sha]
        if merged != remote or join_heads:
            rev = exchange.publish(key, heads, {"files": merged})
            stats["sent"] += 1
        else:
            rev = heads[0] if heads else None
        if downloads:
            stats["received"] += 1
        state.update(revision=rev, cache=cache)
