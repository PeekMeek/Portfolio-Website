"""Portfolio Updater: adds a new model version to your portfolio site.

Run with Windows Python 3 (tkinter is included). No extra packages needed.
1. Set the portfolio folder (the one containing index.html) and, optionally,
   the folder your CAD program exports STEP files to.
2. Tick "Watch for new exports", or use "Add a version manually".
3. When a STEP file appears, answer "What did you change?" and save.
The app copies the file into projects/<project>/, detects which parts changed
since the previous version, and updates data/projects.json.
"""
import hashlib, json, os, re, shutil, subprocess, threading
from collections import defaultdict
from datetime import date
from pathlib import Path

CONFIG = Path(os.environ.get("APPDATA", Path.home())) / "portfolio_updater" / "config.json"
NEW = "+ New project"


# ---------- STEP analysis (pure Python, no dependencies) ----------
def part_hashes(path):
    """Return {part name: geometry fingerprint}. Fingerprints ignore entity
    numbering, so an unchanged part gets the same value on every export."""
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    m = re.search(r"\bDATA\s*;(.*?)\bENDSEC\s*;", text, re.S)
    data = re.sub(r"/\*.*?\*/", "", m.group(1) if m else text, flags=re.S)
    ents, start = {}, 0
    for t in re.finditer(r"'(?:[^']|'')*'|;", data):  # ';' inside quotes is skipped
        if t.group() != ";":
            continue
        e = re.match(r"\s*#(\d+)\s*=\s*(.*)", data[start:t.start()], re.S)
        start = t.end()
        if e:
            ents[e.group(1)] = e.group(2).strip()
    typ, refs, users = {}, {}, defaultdict(list)
    for k, v in ents.items():
        tm = re.match(r"\(?\s*([A-Z0-9_]+)", v)
        typ[k] = tm.group(1) if tm else ""
        refs[k] = re.findall(r"#(\d+)", re.sub(r"'(?:[^']|'')*'", "''", v))
        for r in refs[k]:
            users[r].append(k)

    memo = {}
    def fp(root):  # content hash of an entity and everything it references
        stack, seen = [root], set()
        while stack:
            k = stack[-1]
            if k in memo:
                stack.pop(); continue
            if k not in seen:
                seen.add(k)
                stack.extend(c for c in refs[k] if c in ents and c not in memo and c not in seen)
                continue
            body = re.sub(r"#(\d+)", lambda m: memo.get(m.group(1), "?")[:12], ents[k])
            memo[k] = hashlib.sha1(body.encode()).hexdigest()
            stack.pop()
        return memo[root]

    def up(ids, ok):
        return [u for i in ids for u in users[i] if ok(typ[u])]

    out = {}
    for pid, body in ents.items():
        if typ[pid] != "PRODUCT":
            continue
        strs = re.findall(r"'((?:[^']|'')*)'", body)
        name = (strs[1] if len(strs) > 1 else "") or (strs[0] if strs else "") or "Part " + pid
        forms = up([pid], lambda t: t.startswith("PRODUCT_DEFINITION_FORMATION"))
        defs = up(forms, lambda t: t == "PRODUCT_DEFINITION")
        shapes = up(defs, lambda t: t == "PRODUCT_DEFINITION_SHAPE")
        links = up(shapes, lambda t: t == "SHAPE_DEFINITION_REPRESENTATION")
        reps = [refs[s][1] for s in links if len(refs[s]) > 1 and refs[s][1] in ents]
        if not reps:
            continue
        digest = hashlib.sha1("".join(sorted(fp(r) for r in reps)).encode()).hexdigest()
        n, unique = 2, name
        while unique in out:
            unique = f"{name} ({n})"; n += 1
        out[unique] = digest
    return out


def diff(old, new):
    changes = {n: "added" for n in new if n not in old}
    changes.update({n: "modified" for n in new if n in old and new[n] != old[n]})
    return changes, sorted(n for n in old if n not in new)


# ---------- Portfolio data ----------
def load_data(root):
    f = Path(root) / "data" / "projects.json"
    return json.loads(f.read_text("utf-8")) if f.exists() else {"site": {}, "projects": []}


def save_data(root, data):
    f = Path(root) / "data" / "projects.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    if f.exists():
        shutil.copy2(f, f.with_name("projects.json.bak"))
    f.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", "utf-8")


def slug(title):
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-") or "project"


def add_version(root, proj, note, src):
    """Copy the STEP file in, work out what changed, append the version."""
    root = Path(root)
    vs = proj["versions"]
    vid = f"v{len(vs) + 1}"
    rel = f"projects/{proj['id']}/{vid}.step"
    dest = root / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    new = part_hashes(dest)
    dest.with_suffix(".parts.json").write_text(json.dumps(new, indent=1), "utf-8")
    v = {"id": vid, "date": date.today().isoformat(), "note": note, "file": rel}
    msg = f"{vid}: {len(new)} parts found."
    if not new:
        msg += " No named parts were found, so no change highlighting."
    elif vs and "file" in vs[-1]:
        prev = root / vs[-1]["file"]
        side = prev.with_suffix(".parts.json")
        old = json.loads(side.read_text("utf-8")) if side.exists() else (part_hashes(prev) if prev.exists() else None)
        if old is not None:
            v["changes"], removed = diff(old, new)
            if removed:
                v["removed"] = removed
            c = v["changes"]
            msg += f" Added {sum(x == 'added' for x in c.values())}, modified {sum(x == 'modified' for x in c.values())}, removed {len(removed)}."
    vs.append(v)
    return msg


def publish(root, message, report):
    """git add/commit/push in the background. Needs git installed and a repo."""
    def run():
        flags = 0x08000000 if os.name == "nt" else 0
        for cmd in (["git", "add", "-A"], ["git", "commit", "-m", message], ["git", "push"]):
            try:
                r = subprocess.run(cmd, cwd=root, capture_output=True, text=True, creationflags=flags)
            except OSError:
                return report("Publish failed: git is not installed or not on PATH.")
            if r.returncode and cmd[1] != "commit":  # commit exits 1 when nothing changed
                return report("Publish failed: " + (r.stderr.strip() or r.stdout.strip())[:300])
        report("Published.")
    threading.Thread(target=run, daemon=True).start()


# ---------- GUI ----------
import tkinter as tk
from tkinter import filedialog, messagebox, ttk


class Prompt(tk.Toplevel):
    def __init__(self, app, path):
        super().__init__(app.root)
        self.app, self.path = app, path
        self.title("What did you change?")
        self.resizable(False, False)
        pad = {"padx": 10, "pady": 4, "sticky": "w"}
        ttk.Label(self, text=f"New export: {path.name}", font=("Segoe UI", 10, "bold")).grid(row=0, column=0, columnspan=2, **pad)
        names = [p["title"] for p in app.data()["projects"]]
        ttk.Label(self, text="Project").grid(row=1, column=0, **pad)
        self.proj = ttk.Combobox(self, values=names + [NEW], state="readonly", width=38)
        last = app.cfg.get("last")
        self.proj.set(last if last in names else (names[0] if names else NEW))
        self.proj.grid(row=1, column=1, **pad)
        self.proj.bind("<<ComboboxSelected>>", lambda e: self.refresh())
        self.f = {}
        for i, k in enumerate(("Title", "Summary", "Tags")):
            ttk.Label(self, text=k + (" (comma separated)" if k == "Tags" else "")).grid(row=2 + i, column=0, **pad)
            self.f[k] = ttk.Entry(self, width=41)
            self.f[k].grid(row=2 + i, column=1, **pad)
        ttk.Label(self, text="What did you change?").grid(row=5, column=0, **pad)
        self.note = tk.Text(self, width=40, height=5, wrap="word")
        self.note.grid(row=5, column=1, **pad)
        b = ttk.Frame(self)
        b.grid(row=6, column=0, columnspan=2, pady=8)
        ttk.Button(b, text="Save version", command=self.ok).pack(side="left", padx=6)
        ttk.Button(b, text="Skip", command=self.destroy).pack(side="left")
        self.refresh()
        self.transient(app.root); self.grab_set(); self.lift()
        self.attributes("-topmost", True)
        self.note.focus_set()
        app.root.wait_window(self)

    def refresh(self):
        state = "normal" if self.proj.get() == NEW else "disabled"
        for e in self.f.values():
            e.config(state=state)

    def ok(self):
        note, new = self.note.get("1.0", "end").strip(), self.proj.get() == NEW
        if not note:
            return messagebox.showwarning("Missing note", "Describe what you changed.", parent=self)
        if new and not self.f["Title"].get().strip():
            return messagebox.showwarning("Missing title", "Give the new project a title.", parent=self)
        fields = {k: e.get().strip() for k, e in self.f.items()} if new else None
        if self.app.save(self.path, self.proj.get(), note, fields):
            self.destroy()


class App:
    def __init__(self):
        try:
            self.cfg = json.loads(CONFIG.read_text("utf-8"))
        except (OSError, ValueError):
            self.cfg = {}
        r = self.root = tk.Tk()
        r.title("Portfolio Updater")
        r.geometry("600x420")
        self.v_root = tk.StringVar(value=self.cfg.get("root", ""))
        self.v_watch = tk.StringVar(value=self.cfg.get("watch", ""))
        self.v_on = tk.BooleanVar(value=False)
        self.v_pub = tk.BooleanVar(value=self.cfg.get("publish", True))
        self.seen, self.pending = {}, {}
        f = ttk.Frame(r, padding=12)
        f.pack(fill="both", expand=True)
        f.columnconfigure(1, weight=1)
        for i, (label, var) in enumerate((("Portfolio folder", self.v_root), ("Watch folder", self.v_watch))):
            ttk.Label(f, text=label).grid(row=i, column=0, sticky="w", pady=3)
            ttk.Entry(f, textvariable=var).grid(row=i, column=1, sticky="ew", padx=6)
            ttk.Button(f, text="Browse", command=lambda v=var: self.browse(v)).grid(row=i, column=2)
        ttk.Checkbutton(f, text="Watch for new exports", variable=self.v_on, command=self.toggle).grid(row=2, column=0, columnspan=2, sticky="w", pady=6)
        ttk.Checkbutton(f, text="Publish with git after saving", variable=self.v_pub).grid(row=3, column=0, columnspan=2, sticky="w")
        ttk.Button(f, text="Add a version manually", command=self.manual).grid(row=2, column=2, rowspan=2)
        self.out = tk.Text(f, height=12, state="disabled", wrap="word")
        self.out.grid(row=4, column=0, columnspan=3, sticky="nsew", pady=(10, 0))
        f.rowconfigure(4, weight=1)
        r.protocol("WM_DELETE_WINDOW", self.close)

    def run(self):
        self.root.mainloop()

    def log(self, text):
        self.out.config(state="normal")
        self.out.insert("end", text + "\n")
        self.out.see("end")
        self.out.config(state="disabled")

    def browse(self, var):
        d = filedialog.askdirectory(initialdir=var.get() or None)
        if d:
            var.set(d)

    def close(self):
        self.cfg.update(root=self.v_root.get(), watch=self.v_watch.get(), publish=self.v_pub.get())
        try:
            CONFIG.parent.mkdir(parents=True, exist_ok=True)
            CONFIG.write_text(json.dumps(self.cfg), "utf-8")
        except OSError:
            pass
        self.root.destroy()

    def data(self):
        return load_data(self.v_root.get())

    def ready(self):
        if not (Path(self.v_root.get()) / "index.html").exists():
            messagebox.showwarning("Portfolio folder", "Choose the portfolio folder (the one containing index.html).")
            return False
        return True

    def toggle(self):
        if not self.v_on.get():
            return self.log("Stopped watching.")
        if not Path(self.v_watch.get()).is_dir() or not self.ready():
            self.v_on.set(False)
            return messagebox.showwarning("Watch folder", "Choose an existing watch folder first.")
        self.seen = {p: self.sig(p) for p in self.steps()}  # existing files don't trigger the prompt
        self.pending = {}
        self.log("Watching " + self.v_watch.get())
        self.poll()

    @staticmethod
    def sig(p):
        s = p.stat()
        return (s.st_mtime, s.st_size)

    def steps(self):
        return [p for p in Path(self.v_watch.get()).iterdir() if p.suffix.lower() in (".step", ".stp")]

    def poll(self):
        if not self.v_on.get():
            return
        try:
            for p in self.steps():
                s = self.sig(p)
                if self.seen.get(p) == s:
                    continue
                if self.pending.get(p) == s:  # unchanged for one poll: export has finished
                    self.seen[p] = s
                    self.pending.pop(p)
                    self.root.deiconify(); self.root.lift()
                    Prompt(self, p)
                else:
                    self.pending[p] = s
        except OSError as e:
            self.log(f"Watch error: {e}")
        self.root.after(2000, self.poll)

    def manual(self):
        if not self.ready():
            return
        f = filedialog.askopenfilename(filetypes=[("STEP files", "*.step *.stp")])
        if f:
            Prompt(self, Path(f))

    def save(self, path, choice, note, new):
        root = Path(self.v_root.get())
        try:
            data = load_data(root)
            if new:
                ids = {p["id"] for p in data["projects"]}
                pid, n = slug(new["Title"]), 2
                while pid in ids:
                    pid = f"{slug(new['Title'])}-{n}"; n += 1
                proj = {"id": pid, "title": new["Title"], "summary": new["Summary"],
                        "tags": [t.strip() for t in new["Tags"].split(",") if t.strip()], "versions": []}
                data["projects"].append(proj)
            else:
                proj = next(p for p in data["projects"] if p["title"] == choice)
            msg = add_version(root, proj, note, path)
            save_data(root, data)
        except Exception as e:
            messagebox.showerror("Could not save", str(e))
            return False
        self.cfg["last"] = proj["title"]
        self.log(f"Saved {proj['title']}. {msg}")
        if self.v_pub.get():
            if (root / ".git").exists():
                self.log("Publishing...")
                publish(root, f"{proj['title']}: {note[:60]}", lambda t: self.root.after(0, self.log, t))
            else:
                self.log("Not published: the portfolio folder is not a git repository.")
        return True


if __name__ == "__main__":
    App().run()
