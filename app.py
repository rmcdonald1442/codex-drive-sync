"""Codex Drive Sync desktop UI. Requires Python 3.14+ with Tk."""

import datetime as dt
import os
import queue
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

if sys.version_info < (3, 14):
    raise SystemExit("Install Python 3.14 or newer with Tk to run Codex Drive Sync.")

from engine import INTERVAL, Engine, SyncCancelled, project_info
from storage import MARKER, read_json, write_json

BG, PANEL, INK, MUTED, BLUE = "#111723", "#192233", "#edf3fc", "#a2b1c7", "#69b1ff"
FONT = "Segoe UI" if os.name == "nt" else "Helvetica Neue"


def user_data():
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData/Local") / "Codex Drive Sync"
    return Path.home() / "Library/Application Support/Codex Drive Sync"


def lock_app(folder):
    folder.mkdir(parents=True, exist_ok=True)
    file = (folder / "running.lock").open("a+b")
    try:
        if os.name == "nt":
            import msvcrt
            if file.tell() == 0:
                file.write(b"\0")
                file.flush()
            file.seek(0)
            msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        file.close()
        raise
    return file


class App:
    def __init__(self, root, directory=None, home=None):
        self.root = root
        self.directory = directory or user_data()
        self.settings_path = self.directory / "settings.json"
        self.settings = read_json(self.settings_path, {})
        self.home = home or Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
        self.engine = self.make_engine()
        self.folder = Path(self.settings["drive"]) if self.settings.get("drive") else None
        self.projects, self.chats, self.selections, self.edits = {}, {}, {}, {}
        self.setup = False
        self.ready = False
        self.busy = False
        self.closing = False
        self.cancel_event = None
        self.phase = ""
        self.started_at = 0.0
        self.messages = queue.Queue()
        self.status = tk.StringVar(value="Reading local Codex history…")
        self.caption = tk.StringVar(value="Local Codex projects and chats")
        self.folder_text = tk.StringVar(value=str(self.folder) if self.folder else "Choose a dedicated Google Drive folder")
        self.search = tk.StringVar()
        self.row_marked = tk.BooleanVar(value=False)
        self.project_files = tk.BooleanVar(value=False)
        self.notice = tk.StringVar(value="")
        self.configure()
        self.layout()
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.after(100, self.poll)
        self.root.after(INTERVAL * 1000, self.tick)
        self.start(sync=bool(self.folder and (self.folder / MARKER).is_file()))

    def make_engine(self):
        default = Path.home() / "Codex Sync Projects"
        return Engine(self.home, self.directory / "shares",
                      Path(self.settings.get("workspace_base") or default))

    def configure(self):
        self.root.title("Codex Drive Sync")
        self.root.geometry("1050x725")
        self.root.minsize(740, 540)
        self.root.configure(bg=BG)
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure(".", background=BG, foreground=INK, font=(FONT, 11))
        style.configure("TFrame", background=BG)
        style.configure("TLabel", background=BG, foreground=INK)
        style.configure("Muted.TLabel", foreground=MUTED)
        style.configure("Accent.TLabel", foreground=BLUE)
        style.configure("Title.TLabel", font=(FONT, 27, "bold"))
        style.configure("TButton", background=PANEL, foreground=INK, bordercolor="#35455f", padding=(12, 7))
        style.map("TButton", background=[("active", "#2a3a52")], foreground=[("disabled", "#6d7c90")])
        style.configure("Primary.TButton", background="#2680d9", bordercolor="#2680d9", foreground="white")
        style.map("Primary.TButton", background=[("active", "#3494ed"), ("disabled", "#203451")])
        style.configure("TEntry", fieldbackground=PANEL, foreground=INK, insertcolor=INK, padding=8)
        style.configure("Treeview", background=PANEL, fieldbackground=PANEL, foreground=INK, rowheight=29,
                        bordercolor="#35455f")
        style.configure("Treeview.Heading", background="#233148", foreground=INK, padding=(7, 6))
        style.map("Treeview", background=[("selected", "#2e5378")], foreground=[("selected", "white")])
        style.configure("TCheckbutton", background=BG, foreground=INK)
        style.map("TCheckbutton", background=[("active", BG)], foreground=[("disabled", MUTED)])

    def layout(self):
        body = ttk.Frame(self.root, padding=(24, 20))
        body.pack(fill="both", expand=True)

        # Give the actions their full space before laying out the scrollable list.
        footer = ttk.Frame(body)
        footer.pack(side="bottom", fill="x", pady=(9, 0))
        self.refresh_button = ttk.Button(footer, text="Refresh list", command=lambda: self.start(sync=False))
        self.refresh_button.pack(side="left")
        self.sync_button = ttk.Button(footer, text="Sync now", width=11, command=self.sync_action)
        self.sync_button.pack(side="right")
        self.apply_button = ttk.Button(footer, text="Apply selections", style="Primary.TButton", command=self.apply)
        self.apply_button.pack(side="right", padx=8)
        self.details_button = ttk.Button(footer, text="Sync details", command=self.details)
        self.details_button.pack(side="right", padx=8)
        self.issues = []

        controls = ttk.Frame(body)
        controls.pack(side="bottom", fill="x", pady=(8, 0))
        row = ttk.Frame(controls)
        row.pack(fill="x")
        self.mark_button = ttk.Checkbutton(row, text="Mark highlighted row for sync", variable=self.row_marked,
                                          command=lambda: self.toggle(self.tree.focus()))
        self.mark_button.pack(side="left")
        self.files_button = ttk.Checkbutton(row, text="Include project files", variable=self.project_files,
                                           command=self.set_files)
        self.files_button.pack(side="left", padx=(16, 0))
        self.status_line = ttk.Label(controls, textvariable=self.status, wraplength=690)
        self.status_line.pack(anchor="w", pady=(9, 0))
        self.issue_line = ttk.Label(controls, textvariable=self.notice, foreground="#f2bd79", wraplength=690)
        self.issue_line.pack(anchor="w")

        header = ttk.Frame(body)
        header.pack(fill="x")
        ttk.Label(header, text="Codex Drive Sync", style="Title.TLabel").pack(side="left")
        self.interval = ttk.Label(header, text="●  Checks every 5 minutes while open", style="Accent.TLabel")
        self.interval.pack(side="right")
        ttk.Label(body, text="Take selected projects and chats with you between Windows and Mac.",
                  style="Muted.TLabel").pack(anchor="w", pady=(0, 16))
        connect = ttk.Frame(body)
        connect.pack(fill="x", pady=(0, 8))
        self.browse_button = ttk.Button(connect, text="Choose folder…", command=self.choose_drive)
        self.browse_button.pack(side="right")
        ttk.Label(connect, text="Drive folder", width=14).pack(side="left")
        ttk.Label(connect, textvariable=self.folder_text, style="Muted.TLabel").pack(side="left", fill="x", expand=True)
        workspace = ttk.Frame(body)
        workspace.pack(fill="x", pady=(0, 16))
        self.workspace_button = ttk.Button(workspace, text="Choose location…", command=self.choose_workspace)
        self.workspace_button.pack(side="right")
        ttk.Label(workspace, text="Restore to", width=14).pack(side="left")
        self.workspace_text = tk.StringVar(value=str(self.engine.workspace_base))
        ttk.Label(workspace, textvariable=self.workspace_text, style="Muted.TLabel").pack(side="left", fill="x", expand=True)
        toolbar = ttk.Frame(body)
        toolbar.pack(fill="x", pady=(0, 7))
        self.search_field = ttk.Entry(toolbar, textvariable=self.search, width=19)
        self.search_field.pack(side="right")
        ttk.Label(toolbar, text="Filter", style="Muted.TLabel").pack(side="right", padx=8)
        ttk.Label(toolbar, textvariable=self.caption).pack(side="left", fill="x", expand=True)
        self.search.trace_add("write", lambda *_: self.render())
        table = ttk.Frame(body)
        table.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(table, columns=("kind", "sync"), selectmode="browse", height=1)
        self.tree.heading("#0", text="Project / chat", anchor="w")
        self.tree.heading("kind", text="Type", anchor="w")
        self.tree.heading("sync", text="Sync", anchor="w")
        self.tree.column("#0", width=555, minwidth=290)
        self.tree.column("kind", width=130, minwidth=90, stretch=False)
        self.tree.column("sync", width=160, minwidth=120, stretch=False)
        scroll = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.tree.tag_configure("folder", foreground=BLUE)
        self.tree.bind("<ButtonRelease-1>", self.clicked)
        self.tree.bind("<space>", self.keyboard_toggle)
        self.tree.bind("<<TreeviewSelect>>", lambda _: self.update_row_controls())
        footer.bind("<Configure>", lambda _: self.root.minsize(max(740, footer.winfo_reqwidth() + 48), 540))
        self.update_controls()

    def combined(self):
        result = self.selections.copy()
        for key, value in self.edits.items():
            if value is None:
                result.pop(key, None)
            else:
                result[key] = value
        return result

    def render(self):
        selected = self.combined()
        filt = self.search.get().strip().casefold()
        prior = self.tree.focus()
        view = self.tree.yview()[0]
        opened = {p: self.tree.item(p, "open") for p in self.tree.get_children()}
        for item in self.tree.get_children():
            self.tree.delete(item)
        counts = (sum(v["kind"] == "project" for v in selected.values()),
                  sum(v["kind"] == "chat" for v in selected.values()))
        self.caption.set(f"{len(self.projects)} projects · {len(self.chats)} chats    |    "
                         f"{counts[0]} projects and {counts[1]} individual chats selected")
        groups = {k: [] for k in self.projects}
        groups[""] = []
        for chat in sorted(self.chats.values(), key=lambda c: c.updated, reverse=True):
            groups.get(chat.project, groups[""]).append(chat)
        project_rows = {ident: p.title for ident, p in self.projects.items()}
        project_rows.update({k: v.get("title", k) for k, v in selected.items()
                             if v["kind"] == "project" and k not in project_rows})
        for ident, title in sorted(project_rows.items(), key=lambda pair: pair[1].casefold()) + [("", "Unassigned chats")]:
            key = f"p:{ident}"
            items = groups.get(ident, [])
            match_title = filt in title.casefold()
            visible = [c for c in items if match_title or filt in c.title.casefold()]
            if (not ident and not visible) or (not match_title and not visible):
                continue
            value = selected.get(ident)
            kind = "Project" if ident else "Group"
            state = ("✓ Chats + files" if value.get("files") else "✓ All chats") if value else ("☐ Select" if ident else "")
            self.tree.insert("", "end", key, text=f"{title}  ({len(items)})", values=(kind, state), tags=("folder",),
                             open=bool(filt) or bool(opened.get(key, not ident)))
            for chat in visible:
                state = "✓ Via project" if value else ("✓ Selected" if chat.id in selected else "☐ Select")
                self.tree.insert(key, "end", f"c:{chat.id}", text=chat.title,
                                 values=("Archived chat" if chat.archived else "Chat", state))
        for ident, value in selected.items():
            if value["kind"] == "chat" and ident not in self.chats:
                title = value.get("title", ident)
                if filt in title.casefold():
                    self.tree.insert("", "end", f"c:{ident}", text=title, values=("Drive chat", "✓ Selected"))
        if prior and self.tree.exists(prior):
            self.tree.focus(prior)
            self.tree.selection_set(prior)
            self.tree.yview_moveto(view)
        self.update_controls()

    def update_controls(self):
        idle = self.ready and not self.busy and not self.closing
        for button in (self.browse_button, self.workspace_button, self.refresh_button):
            button.configure(state="normal" if idle else "disabled")
        can_stop = self.cancel_event is not None and not self.cancel_event.is_set()
        self.sync_button.configure(text="Stop sync" if self.cancel_event is not None else "Sync now",
                                   state="normal" if can_stop or (idle and self.setup) else "disabled")
        self.apply_button.configure(state="normal" if idle and self.folder and self.edits else "disabled")
        self.details_button.configure(state="normal" if self.issues else "disabled")
        self.update_row_controls()

    def update_row_controls(self):
        row = self.tree.focus()
        selected = self.combined()
        value = selected.get(row[2:])
        is_project = row.startswith("p:") and row != "p:"
        is_chat = row.startswith("c:")
        chat = self.chats.get(row[2:]) if is_chat else None
        inherited = bool(chat and chat.project in selected)
        self.row_marked.set(bool(value) or inherited)
        title = "Sync this project" if is_project else "Sync this chat" if is_chat else "Mark highlighted row for sync"
        idle = self.ready and not self.busy and not self.closing
        self.mark_button.configure(text=title, state="normal" if idle and (is_project or is_chat)
                                   and not inherited else "disabled")
        self.project_files.set(bool(is_project and value and value.get("files")))
        self.files_button.configure(state="normal" if idle and is_project and value else "disabled")

    def edit(self, ident, value):
        if self.selections.get(ident) == value:
            self.edits.pop(ident, None)
        else:
            self.edits[ident] = value
        if self.edits and not self.folder:
            self.status.set("Choose a Drive folder to save these selections.")
        elif self.edits:
            self.status.set("Selection changes are pending. Click Apply selections to save them to Drive.")
        else:
            self.status.set("Selections match the saved setup.")
        self.render()

    def clicked(self, event):
        if self.tree.identify_region(event.x, event.y) == "cell" and self.tree.identify_column(event.x) == "#2":
            self.toggle(self.tree.identify_row(event.y))

    def keyboard_toggle(self, _event):
        self.toggle(self.tree.focus())
        return "break"

    def toggle(self, row):
        if self.busy or not row or row == "p:":
            return
        kind, ident = row.split(":", 1)
        selected = self.combined()
        if kind == "c":
            chat = self.chats.get(ident)
            if chat and selected.get(chat.project, {}).get("kind") == "project":
                self.status.set("This chat is included by its project.")
                return
            if chat:
                self.edit(ident, None if ident in selected else {"kind": "chat", "title": chat.title})
            elif ident in selected:
                self.edit(ident, None)
        elif ident in selected:
            self.edit(ident, None)
        elif ident in self.projects:
            self.edit(ident, {"kind": "project", **project_info(self.projects[ident]), "files": False})

    def set_files(self):
        ident = self.tree.focus()[2:]
        value = self.combined().get(ident)
        if value and value["kind"] == "project":
            self.edit(ident, {**value, "files": self.project_files.get()})

    def choose_drive(self):
        value = filedialog.askdirectory(parent=self.root, title="Choose a dedicated Google Drive folder",
                                        initialdir=str(self.folder) if self.folder and self.folder.exists()
                                        else ("G:/My Drive" if Path("G:/My Drive").is_dir() else str(Path.home())))
        if value:
            if self.setup and Path(value) != self.folder:
                self.edits.clear()
            self.folder = Path(value)
            self.folder_text.set(value)
            self.settings["drive"] = value
            write_json(self.settings_path, self.settings)
            self.start(sync=(self.folder / MARKER).is_file())

    def choose_workspace(self):
        start = self.engine.workspace_base
        value = filedialog.askdirectory(parent=self.root, title="Choose a location for newly restored projects",
                                        initialdir=str(start if start.is_dir() else Path.home()))
        if value:
            self.settings["workspace_base"] = value
            self.engine = self.make_engine()
            self.workspace_text.set(str(self.engine.workspace_base))
            write_json(self.settings_path, self.settings)

    def apply(self):
        if self.folder and self.edits:
            self.start(sync=True, edits=self.edits.copy())

    def sync_action(self):
        if self.cancel_event is not None:
            self.cancel_event.set()
            self.phase = "Stopping after the current operation…"
            self.status.set(self.phase)
            self.update_controls()
        else:
            self.start(sync=True)

    def tick(self):
        if not self.closing:
            if self.folder and not self.busy:
                self.start(sync=self.setup or (self.folder / MARKER).is_file())
            self.root.after(INTERVAL * 1000, self.tick)

    def start(self, sync, edits=None):
        if self.busy or self.closing:
            return
        self.busy = True
        cancel = threading.Event() if sync else None
        self.cancel_event = cancel
        self.phase = "Checking Drive and local history…" if sync else "Reading local projects and chats…"
        self.started_at = time.monotonic()
        self.status.set(self.phase)
        self.update_controls()
        folder, engine = self.folder, self.engine
        def work():
            stats, error = None, None
            stopped = False
            if sync:
                try:
                    stats = engine.run(folder, edits, lambda message: self.messages.put(("status", message)),
                                       cancelled=cancel.is_set)
                except SyncCancelled:
                    stopped = True
                except Exception as exc:
                    error = str(exc)
            self.messages.put(("status", "Refreshing the list…"))
            try:
                result = engine.inspect(folder)
            except Exception as exc:
                result = None
                error = error or str(exc)
            self.messages.put(("done", result, stats, error, edits, stopped))
        threading.Thread(target=work, name="codex-sync", daemon=True).start()

    def poll(self):
        while not self.messages.empty():
            message = self.messages.get()
            if message[0] == "status":
                if not self.closing and not (self.cancel_event and self.cancel_event.is_set()):
                    self.phase = message[1]
                continue
            _, result, stats, error, edits, stopped = message
            self.busy, self.ready = False, True
            self.cancel_event = None
            if result:
                self.projects, self.chats, self.selections, self.setup = result
                if edits:
                    for ident, value in edits.items():
                        if self.selections.get(ident) == value:
                            self.edits.pop(ident, None)
            self.issues = ([error] if error else []) + (stats["issues"] if stats else [])
            issue = self.issues[0] if self.issues else ""
            if len(issue) > 140:
                issue = issue[:137] + "…"
            self.notice.set(f"{issue}  See Sync details." if issue else "")
            if stats:
                clock = dt.datetime.now().strftime("%H:%M")
                summary = f"Last checked {clock} · {stats['sent']} sent · {stats['received']} received"
                if stats["waiting"]:
                    summary += f" · {stats['waiting']} waiting. Quit Codex, then click Sync now or wait for the next check."
                self.status.set(summary)
            elif error:
                self.status.set("Sync could not finish. See Sync details.")
            elif stopped:
                self.status.set("Sync stopped. Use Sync now to continue.")
            elif self.edits:
                self.status.set("Click Apply selections to save your changes to Drive." if self.folder
                                else "Choose a Drive folder to save these selections.")
            elif self.setup:
                self.status.set("Shared setup loaded. Use the Sync column to change selections.")
            elif self.folder:
                self.status.set("Tick the Sync checkbox next to a project or chat to enable Apply selections.")
            else:
                self.status.set("Choose a Drive folder, then tick the projects or chats to sync.")
            self.render()
            if self.closing:
                self.root.destroy()
                return
        if self.busy:
            minutes, seconds = divmod(int(time.monotonic() - self.started_at), 60)
            status = f"{self.phase}  ·  {minutes}:{seconds:02d}"
            if status != self.status.get():
                self.status.set(status)
        self.root.after(100, self.poll)

    def details(self):
        window = tk.Toplevel(self.root)
        window.title("Sync details")
        window.geometry("690x350")
        text = tk.Text(window, wrap="word", background=PANEL, foreground=INK, padx=14, pady=12, font=(FONT, 11))
        text.pack(fill="both", expand=True)
        text.insert("1.0", "\n\n".join(self.issues))
        text.configure(state="disabled")

    def close(self):
        if self.busy:
            self.closing = True
            if self.cancel_event:
                self.cancel_event.set()
            self.phase = "Finishing the current operation before closing…"
            self.status.set(self.phase)
            self.update_controls()
        else:
            self.root.destroy()


def main():
    root = tk.Tk()
    try:
        lock = lock_app(user_data())
    except OSError:
        root.withdraw()
        messagebox.showinfo("Codex Drive Sync", "The sync app is already running or its settings folder is unavailable.")
        root.destroy()
        return
    with lock:
        App(root)
        root.mainloop()


if __name__ == "__main__":
    main()
