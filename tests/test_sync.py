import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest
import uuid
from compression import zstd
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine import Engine, SyncCancelled, project_info, workspace_versions
from native import AppServer, catalog, executable, remap
from storage import Exchange, SyncError, safe_path, write_json


STAMP = "2026-10-20T12:00:00.000Z"


def event(ordinal, kind, data):
    return (json.dumps({"timestamp": STAMP, "ordinal": ordinal, "type": kind, "payload": data},
                       ensure_ascii=False, separators=(",", ":")) + "\n").encode()


def make_chat(home, origin, ident=None, compressed=False, archived=False):
    ident = ident or str(uuid.uuid4())
    directory = home / "archived_sessions" if archived else home / "sessions/2026/10/20"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"rollout-2026-10-20T12-00-00-{ident}.jsonl"
    data = event(0, "session_meta", {"id": ident, "timestamp": STAMP, "cwd": str(origin),
                                    "runtime_workspace_roots": [str(origin)], "originator": "Codex Desktop",
                                    "source": "vscode", "cli_version": "0.166.0",
                                    "model_provider": "openai", "history_mode": "paginated"})
    data += event(1, "event_msg", {"type": "user_message", "message": "Portable smoke test",
                                   "images": [], "local_images": [], "text_elements": []})
    data += event(2, "response_item", {"type": "message", "role": "user",
                                      "content": [{"type": "input_text", "text": "Portable smoke test"}]})
    if compressed:
        path = path.with_suffix(".jsonl.zst")
        data = zstd.compress(data)
    path.write_bytes(data)
    return ident, path


class SyncTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="codex-drive-sync-test-")
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.a = Engine(root / "codex-a", root / "state-a", root / "projects-a")
        self.b = Engine(root / "codex-b", root / "state-b", root / "projects-b")
        self.workspace = self.a.workspace_base / "demo"
        self.workspace.mkdir(parents=True)
        self.drive = root / "drive"
        self.drive.mkdir()
        self.pid = str(uuid.uuid4())
        write_json(self.a.home / ".codex-global-state.json", {
            "local-projects": {self.pid: {"name": "Demo", "rootPaths": [str(self.workspace)]}}})
        write_json(self.b.home / ".codex-global-state.json", {"local-projects": {}, "keep-other-settings": True})

    def choose(self, files=True):
        return {self.pid: {"kind": "project", **project_info(catalog(self.a.home)[0][self.pid]), "files": files}}

    def test_windows_to_mac_path_translation_and_no_text_replacement(self):
        ident, path = make_chat(self.a.home, r"C:\Code\Example\sub", compressed=True)
        target = self.a.local_dir / "plain.jsonl"
        target.parent.mkdir()
        remap(path, target, ident, r"C:\Code\Example", self.workspace)
        lines = [json.loads(line) for line in target.read_bytes().splitlines()]
        self.assertEqual(lines[0]["payload"]["cwd"], str(self.workspace / "sub"))
        self.assertEqual(lines[2]["payload"]["content"][0]["text"], "Portable smoke test")
        self.assertEqual([line["ordinal"] for line in lines], [0, 1, 2])

    def test_shared_config_and_incremental_workspace_updates(self):
        (self.workspace / "first.txt").write_text("a")
        (self.workspace / ".env").write_text("do not copy")
        with patch("engine.codex_open", return_value=True):
            first = self.a.run(self.drive, self.choose())
            self.assertEqual(first["issues"], [])
            self.assertEqual(first["sent"], 1)
            pulled = self.b.run(self.drive)
            self.assertEqual(pulled["issues"], [])
            path = next(self.b.workspace_base.rglob("first.txt"))
            self.assertEqual(path.read_text(), "a")
            self.assertFalse((path.parent / ".env").exists())
            self.assertEqual(self.a.run(self.drive)["sent"], 0)
            path.write_text("b")
            self.assertEqual(self.b.run(self.drive)["sent"], 1)
            self.assertEqual(self.a.run(self.drive)["received"], 1)
            self.assertEqual((self.workspace / "first.txt").read_text(), "b")
            self.a.run(self.drive, {self.pid: None})
            self.assertEqual(Exchange(self.drive).selections(), {})
            self.assertTrue(path.exists())

    def test_conflicting_working_files_keep_both(self):
        source = self.workspace / "file.txt"
        source.write_text("initial")
        with patch("engine.codex_open", return_value=True):
            self.a.run(self.drive, self.choose())
            self.b.run(self.drive)
            target = next(self.b.workspace_base.rglob("file.txt"))
            target.write_text("b")
            source.write_text("a")
            self.a.run(self.drive)
            result = self.b.run(self.drive)
            self.assertIn("Files changed on both computers", result["issues"][0])
            self.assertEqual(target.read_text(), "b")
            self.assertEqual(source.read_text(), "a")

    @unittest.skipUnless(shutil.which("git"), "Git is required to evaluate .gitignore")
    def test_standalone_gitignore_filters_new_uploads_and_old_downloads(self):
        runs = self.workspace / "runs"
        runs.mkdir()
        (runs / "results.json").write_text("generated")
        nested = self.workspace / "nested"
        nested.mkdir()
        (nested / ".gitignore").write_text("*.json\n!keep.json\n")
        (nested / "keep.json").write_text("keep me")
        (nested / "discard.json").write_text("skip me")
        with patch("engine.codex_open", return_value=True):
            # The old workspace inventory predates the root ignore rule.
            self.a.run(self.drive, self.choose())
            receiver = self.b.workspace_base / f"Demo-{self.pid[-16:]}"
            receiver.mkdir(parents=True)
            (receiver / ".gitignore").write_text("runs/\n")
            result = self.b.run(self.drive)
            self.assertEqual(result["issues"], [])
            self.assertTrue((receiver / "nested" / "keep.json").is_file())
            self.assertFalse((receiver / "nested" / "discard.json").exists())
            self.assertFalse((receiver / "runs").exists())

            (self.workspace / ".gitignore").write_text("runs/\n")
            fresh = self.drive.parent / "fresh"
            fresh.mkdir()
            result = self.a.run(fresh, self.choose())
            self.assertEqual(result["issues"], [])
            key = "w-" + hashlib.sha256(self.pid.encode()).hexdigest()
            versions, heads, _ = Exchange(fresh).history(key)
            files = versions[heads[0]]["files"]
            self.assertEqual(set(files), {"0/.gitignore", "0/nested/.gitignore", "0/nested/keep.json"})
            self.assertFalse((self.workspace / ".git").exists())
            self.assertTrue((runs / "results.json").is_file())

    @unittest.skipUnless(shutil.which("git"), "Git is required to evaluate .gitignore")
    def test_preview_sizes_match_sync_filters_without_reading_file_contents(self):
        rules = b"runs/\n"
        (self.workspace / ".gitignore").write_bytes(rules)
        (self.workspace / "runs").mkdir()
        (self.workspace / "runs" / "report.json").write_bytes(b"x" * 200)
        (self.workspace / ".env").write_bytes(b"x" * 50)
        (self.workspace / "work.py").write_bytes(b"x" * 10)
        project = catalog(self.a.home)[0][self.pid]

        with patch("engine.hash_file", side_effect=AssertionError("preview must use metadata")):
            preview = self.a.preview_workspace(project, self.drive)
        self.assertEqual((preview.eligible.count, preview.eligible.bytes), (2, len(rules) + 10))
        self.assertEqual((preview.ignored.count, preview.ignored.bytes), (2, 250))
        self.assertFalse(preview.partial)
        self.assertEqual(list(self.drive.iterdir()), [])
        self.assertFalse((self.workspace / ".git").exists())

        result = self.a.run(self.drive, self.choose())
        self.assertEqual(result["issues"], [])
        key = "w-" + hashlib.sha256(self.pid.encode()).hexdigest()
        versions, heads, _ = Exchange(self.drive).history(key)
        self.assertEqual(set(versions[heads[0]]["files"]), {"0/.gitignore", "0/work.py"})

    def test_stopping_a_workspace_transfer_keeps_chats_and_allows_retry(self):
        ident, _ = make_chat(self.a.home, self.workspace)
        content = b"workspace payload" * 100_000
        (self.workspace / "large.txt").write_bytes(content)
        messages = []

        def cancelled():
            return any(message.startswith("Sending Demo") for message in messages)

        with self.assertRaises(SyncCancelled):
            self.a.run(self.drive, self.choose(), messages.append, cancelled=cancelled)
        exchange = Exchange(self.drive)
        chat_key = "c-" + hashlib.sha256(ident.encode()).hexdigest()
        workspace_key = "w-" + hashlib.sha256(self.pid.encode()).hexdigest()
        obj = exchange.object_path(hashlib.sha256(content).hexdigest())
        self.assertTrue(exchange.history(chat_key)[1])
        self.assertEqual(exchange.history(workspace_key)[1], [])
        self.assertFalse(obj.exists())
        self.assertEqual(list(obj.parent.glob("*.tmp")), [])
        self.assertTrue(any(message.startswith("Scanning project files") for message in messages))
        retried = self.a.run(self.drive)
        self.assertEqual(retried["issues"], [])
        self.assertEqual(retried["sent"], 1)
        self.assertEqual(obj.read_bytes(), content)

    def test_workspace_path_rejects_traversal_and_windows_devices(self):
        for rel in ("../escape", "nested/../escape", "/absolute", "C:/dir", "x/AUX.txt", "x/name."):
            with self.subTest(rel=rel), self.assertRaises(SyncError):
                safe_path(self.workspace, rel)

    def test_parallel_updates_keep_two_heads(self):
        ex = Exchange(self.drive, create=True)
        parent = ex.publish("c-test", [], {"title": "base"})
        left = ex.publish("c-test", [parent], {"title": "left"})
        right = ex.publish("c-test", [parent], {"title": "right"})
        self.assertEqual(set(ex.history("c-test")[1]), {left, right})

    def test_parallel_workspace_updates_merge_different_paths(self):
        ex = Exchange(self.drive, create=True)
        base = ex.publish("w-test", [], {"files": {"0/a": "x", "0/b": "y"}})
        one = ex.publish("w-test", [base], {"files": {"0/a": "new x", "0/b": "y"}})
        two = ex.publish("w-test", [base], {"files": {"0/a": "x", "0/b": "new y"}})
        _, heads, files, join = workspace_versions(ex, "w-test", {"revision": one})
        self.assertEqual(set(heads), {one, two})
        self.assertEqual(files, {"0/a": "new x", "0/b": "new y"})
        self.assertTrue(join)

    def test_chat_moved_out_of_selected_project_stops_uploading(self):
        ident, path = make_chat(self.a.home, self.workspace)
        self.assertEqual(self.a.run(self.drive, self.choose(files=False))["sent"], 1)
        outside = self.workspace.parent / "unselected"
        outside.mkdir()
        make_chat(self.a.home, outside, ident=ident)
        with path.open("ab") as f:
            f.write(event(3, "event_msg", {"type": "user_message", "message": "Outside scope"}))
        self.assertEqual(self.a.run(self.drive)["sent"], 0)

    @unittest.skipUnless(os.environ.get("CODEX_SYNC_TEST_NATIVE") == "1" and executable(),
                         "Set CODEX_SYNC_TEST_NATIVE=1 to run with an installed Codex app server")
    def test_native_project_chat_restore_and_resume(self):
        ident, path = make_chat(self.a.home, self.workspace)
        archived, _ = make_chat(self.a.home, self.workspace, compressed=True, archived=True)
        self.assertEqual(self.a.run(self.drive, self.choose(files=False))["issues"], [])
        with patch("engine.codex_open", return_value=True):
            self.assertGreater(self.b.run(self.drive)["waiting"], 0)
            self.assertEqual(catalog(self.b.home)[1], {})
        with patch("engine.codex_open", return_value=False):
            result = self.b.run(self.drive)
        self.assertEqual(result["issues"], [])
        self.assertEqual(result["received"], 2)
        p, c, selections, _ = self.b.inspect(self.drive)
        self.assertIn(self.pid, selections)
        self.assertEqual(c[ident].project, self.pid)
        self.assertEqual(c[archived].archived, True)
        self.assertEqual(c[ident].cwd, p[self.pid].roots[0])
        server = AppServer(self.b.home)
        try:
            read = server.call("thread/read", {"threadId": ident})["thread"]
            self.assertTrue(read["projectId"])
            resumed = server.call("thread/resume", {"threadId": ident, "cwd": c[ident].cwd, "excludeTurns": True})
            self.assertEqual(resumed["thread"]["id"], ident)
        finally:
            server.close()
        with path.open("ab") as f:
            f.write(event(3, "event_msg", {"type": "user_message", "message": "Next turn",
                                           "images": [], "local_images": [], "text_elements": []}))
        self.assertEqual(self.a.run(self.drive)["sent"], 1)
        with patch("engine.codex_open", return_value=False):
            update = self.b.run(self.drive)
        self.assertEqual(update["issues"], [])
        self.assertEqual(update["received"], 1)
        self.assertIn(b"Next turn", catalog(self.b.home)[1][ident].path.read_bytes())
        # Continue in the other direction; older source histories can be zstd-compressed.
        zipped = path.with_suffix(".jsonl.zst")
        zipped.write_bytes(zstd.compress(path.read_bytes()))
        path.unlink()
        continued = catalog(self.b.home)[1][ident].path
        with continued.open("ab") as f:
            f.write(event(5, "event_msg", {"type": "user_message", "message": "Return trip",
                                           "images": [], "local_images": [], "text_elements": []}))
        self.assertEqual(self.b.run(self.drive)["sent"], 1)
        with patch("engine.codex_open", return_value=False):
            back = self.a.run(self.drive)
        self.assertEqual(back["issues"], [])
        self.assertEqual(back["received"], 1)
        self.assertIn(b"Return trip", catalog(self.a.home)[1][ident].path.read_bytes())


if __name__ == "__main__":
    unittest.main()
