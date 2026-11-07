import json
import os
import sys
import tempfile
import unittest
import uuid
from compression import zstd
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine import Engine, project_info, workspace_versions
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
