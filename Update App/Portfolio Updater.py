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
        reps = {refs[s][1] for s in links if len(refs[s]) > 1 and refs[s][1] in ents}
        if not reps:
            continue
        # AP242 exports (Designcenter/NX included) often keep the solid in a separate
        # representation linked by SHAPE_REPRESENTATION_RELATIONSHIP. Follow those links,
        # but not placement links (WITH_TRANSFORMATION), which lead to child components.
        todo = list(reps)
        while todo:
            r = todo.pop()
            for u in users[r]:
                if typ[u] != "SHAPE_REPRESENTATION_RELATIONSHIP" or "TRANSFORMATION" in ents[u]:
                    continue
                for o in refs[u]:
                    if o in ents and o not in reps and "REPRESENTATION" in typ[o] and "RELATIONSHIP" not in typ[o]:
                        reps.add(o)
                        todo.append(o)
        digest = hashlib.sha1("".join(sorted(fp(r) for r in reps)).encode()).hexdigest()
        n, unique = 2, name
        while unique in out:
            unique = f"{name} ({n})"; n += 1
        out[unique] = digest
    return out


HASH_FORMAT = 2  # bump when part_hashes changes, so old saved fingerprints are redone


def write_parts(step_file, parts):
    Path(step_file).with_suffix(".parts.json").write_text(
        json.dumps({"format": HASH_FORMAT, "parts": parts}, indent=1), "utf-8")


def saved_parts(root, version):
    """Part fingerprints of a saved version, or None if its file is missing."""
    if "file" not in version:
        return None
    f = Path(root) / version["file"]
    side = f.with_suffix(".parts.json")
    if side.exists():
        try:
            d = json.loads(side.read_text("utf-8"))
            if d.get("format") == HASH_FORMAT:
                return d["parts"]
        except (ValueError, AttributeError, KeyError):
            pass
    if not f.is_file():
        return None
    parts = part_hashes(f)
    write_parts(f, parts)  # refresh fingerprints saved by an older version of this app
    return parts


def recalc_changes(root, proj):
    """Recompute added/modified/removed for every version from the saved STEP files.
    Returns how many versions now show different changes."""
    fixed = 0
    for _, _, vs in windows_of(proj):
        prev = None
        for v in vs:
            cur = saved_parts(root, v)
            before = (v.get("changes"), v.get("removed"))
            v.pop("changes", None)
            v.pop("removed", None)
            if prev is not None and cur:
                v["changes"], removed = diff(prev, cur)
                if removed:
                    v["removed"] = removed
            if (v.get("changes"), v.get("removed")) != before:
                fixed += 1
            prev = cur
    return fixed


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


def set_cover(root, proj, src):
    """Copy a picture in as the project's card image. Returns its size in bytes."""
    src = Path(src)
    d = Path(root) / "projects" / proj["id"]
    d.mkdir(parents=True, exist_ok=True)
    for old in d.glob("cover.*"):
        old.unlink()
    dest = d / ("cover" + src.suffix.lower())
    shutil.copy2(src, dest)
    proj["image"] = f"projects/{proj['id']}/{dest.name}"
    return dest.stat().st_size


def slug(title):
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-") or "project"


def model_key(name):
    """A model is identified by its STEP file name, ignoring case and the .step/.stp ending."""
    return re.sub(r"\.[^.\\/]+$", "", str(name or "")).strip().lower()


def group_of(proj, name):
    """The project's versions that came from a STEP file with this name."""
    k = model_key(name)
    return [v for v in proj["versions"] if v.get("source") and model_key(v["source"]) == k]


def has_unnamed(proj):
    """True if some versions were saved before file names were tracked."""
    return any(not v.get("source") for v in proj["versions"])


def latest_for(proj, name):
    """Latest saved version for this file name, else the latest unnamed (older) one."""
    g = group_of(proj, name)
    if g:
        return g[-1]
    return next((v for v in reversed(proj["versions"]) if not v.get("source")), None)


def windows_of(proj):
    """[(label, key, versions)] for each window, in the order the site shows them."""
    out = {}
    for v in proj["versions"]:
        k = model_key(v.get("source"))
        if k not in out:
            out[k] = [proj["title"] + " (no file name)", []]
        out[k][1].append(v)
        if v.get("source"):
            out[k][0] = v["source"]
    return [(label, k, vs) for k, (label, vs) in out.items()]


def inside(root, path):
    return Path(root).resolve() in Path(path).resolve().parents


def delete_window(root, proj, key):
    """Remove one window's versions and their copied STEP files. Returns the count removed."""
    root = Path(root)
    gone = [v for v in proj["versions"] if model_key(v.get("source")) == key]
    proj["versions"] = [v for v in proj["versions"] if model_key(v.get("source")) != key]
    for v in gone:
        if "file" not in v or not inside(root, root / v["file"]):
            continue
        f = root / v["file"]
        for p in (f, f.with_suffix(".parts.json")):
            try:
                p.unlink()
            except OSError:
                pass
        try:
            f.parent.rmdir()  # only succeeds when the folder is now empty
        except OSError:
            pass
    return len(gone)


def add_version(root, proj, note, src, adopt=False):
    """Copy the STEP file in, work out what changed, append the version.
    Each STEP file name is its own model with its own version history.
    adopt=True claims older, unnamed versions for this file name."""
    root, src = Path(root), Path(src)
    vs = proj["versions"]
    if adopt:
        for old_v in vs:
            if not old_v.get("source"):
                old_v["source"] = src.name
    group = group_of(proj, src.name)
    n = len(group) + 1
    folder = root / "projects" / proj["id"] / slug(Path(src.name).stem)
    while (folder / f"v{n}.step").exists():
        n += 1
    vid = f"v{n}"
    dest = folder / f"{vid}.step"
    rel = dest.relative_to(root).as_posix()
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    new = part_hashes(dest)
    write_parts(dest, new)
    v = {"id": vid, "date": date.today().isoformat(), "note": note, "source": src.name, "file": rel}
    msg = f"{src.name} {vid}: {len(new)} parts found."
    if not group:
        msg += " New model, so it has its own window."
    if not new:
        msg += " No named parts were found, so no change highlighting."
    elif group:
        old = saved_parts(root, group[-1])
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
        def git(*args):
            return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, creationflags=flags)
        try:
            for args in (("add", "-A"), ("commit", "-m", message), ("pull", "--rebase"), ("push",)):
                r = git(*args)
                if r.returncode and args[0] != "commit":  # commit exits 1 when nothing changed
                    detail = (r.stderr.strip() or r.stdout.strip())[:300]
                    if args[0] == "pull":
                        git("rebase", "--abort")  # leave the folder in a clean state
                        return report("Publish failed: could not merge with GitHub's copy. "
                                      "Your version is saved locally. Run 'git pull --rebase' in the "
                                      "portfolio folder to see why. " + detail)
                    return report("Publish failed: " + detail)
        except OSError:
            return report("Publish failed: git is not installed or not on PATH.")
        report("Published.")
    threading.Thread(target=run, daemon=True).start()


# ---------- GUI ----------
import tkinter as tk
from tkinter import filedialog, messagebox, ttk


class Prompt(tk.Toplevel):
    def __init__(self, app, path, project=None):
        super().__init__(app.root)
        self.app, self.path = app, path
        self.title("What did you change?")
        self.resizable(False, False)
        pad = {"padx": 10, "pady": 4, "sticky": "w"}
        ttk.Label(self, text=f"New export: {path.name}", font=("Segoe UI", 10, "bold")).grid(row=0, column=0, columnspan=2, **pad)
        names = [p["title"] for p in app.data()["projects"]]
        ttk.Label(self, text="Project").grid(row=1, column=0, **pad)
        self.proj = ttk.Combobox(self, values=names + [NEW], state="readonly", width=38)
        last = project or app.cfg.get("last")
        self.proj.set(last if last in names else (names[0] if names else NEW))
        self.proj.grid(row=1, column=1, **pad)
        self.proj.bind("<<ComboboxSelected>>", lambda e: self.refresh())
        self.f = {}
        for i, k in enumerate(("Title", "Summary", "Tags")):
            ttk.Label(self, text=k + (" (comma separated)" if k == "Tags" else "")).grid(row=2 + i, column=0, **pad)
            self.f[k] = ttk.Entry(self, width=41)
            self.f[k].grid(row=2 + i, column=1, **pad)
        ttk.Label(self, text="Card picture (optional)").grid(row=5, column=0, **pad)
        pf = ttk.Frame(self)
        pf.grid(row=5, column=1, **pad)
        self.pic = tk.StringVar()
        ttk.Entry(pf, textvariable=self.pic, width=31, state="readonly").pack(side="left")
        ttk.Button(pf, text="Browse", command=self.pick).pack(side="left", padx=(4, 0))
        ttk.Label(self, text="What did you change?").grid(row=6, column=0, **pad)
        self.note = tk.Text(self, width=40, height=5, wrap="word")
        self.note.grid(row=6, column=1, **pad)
        self.hint = ttk.Label(self, wraplength=430, font=("Segoe UI", 9, "bold"))
        self.hint.grid(row=7, column=0, columnspan=2, **pad)
        self.adopt = tk.BooleanVar(value=False)  # a new file name gets its own window unless you say otherwise
        self.adopt_box = ttk.Checkbutton(self, variable=self.adopt, command=self.update_hint,
                                         text="Earlier versions of this project came from this same file")
        self.adopt_box.grid(row=8, column=0, columnspan=2, **pad)
        b = ttk.Frame(self)
        b.grid(row=9, column=0, columnspan=2, pady=8)
        ttk.Button(b, text="Save version", command=self.ok).pack(side="left", padx=6)
        ttk.Button(b, text="Skip", command=self.destroy).pack(side="left")
        self.refresh()
        self.transient(app.root); self.grab_set(); self.lift()
        self.attributes("-topmost", True)
        self.note.focus_set()
        app.root.wait_window(self)

    def pick(self):
        f = filedialog.askopenfilename(parent=self, filetypes=[("Pictures", "*.png *.jpg *.jpeg *.webp *.gif")])
        if f:
            self.pic.set(f)

    def refresh(self):
        state = "normal" if self.proj.get() == NEW else "disabled"
        for e in self.f.values():
            e.config(state=state)
        self.update_hint()

    def selected(self):
        return next((p for p in self.app.data()["projects"] if p["title"] == self.proj.get()), None)

    def can_adopt(self):
        """Older versions have no file name yet, and this name matches no window."""
        p = self.selected()
        return bool(p and has_unnamed(p) and not group_of(p, self.path.name))

    def update_hint(self):
        p, name = self.selected(), self.path.name
        if p is None:
            text = f'A new window for "{name}" will be created.'
        elif group_of(p, name):
            text = f'This adds a version to the "{name}" window.'
        elif self.can_adopt() and self.adopt.get():
            text = f'This adds a version to the project\'s existing window, which will be named "{name}".'
        else:
            text = f'New file name: this creates a new window for "{name}".'
        self.hint.config(text=text)
        if self.can_adopt():
            self.adopt_box.grid()
        else:
            self.adopt_box.grid_remove()

    def ok(self):
        note, new = self.note.get("1.0", "end").strip(), self.proj.get() == NEW
        if not note:
            return messagebox.showwarning("Missing note", "Describe what you changed.", parent=self)
        if new and not self.f["Title"].get().strip():
            return messagebox.showwarning("Missing title", "Give the new project a title.", parent=self)
        fields = {k: e.get().strip() for k, e in self.f.items()} if new else None
        adopt = self.can_adopt() and self.adopt.get()
        if self.app.save(self.path, self.proj.get(), note, fields, self.pic.get(), adopt):
            self.destroy()


class Manage(tk.Toplevel):
    """Pick a project and delete one of its windows."""
    def __init__(self, app):
        super().__init__(app.root)
        self.app = app
        self.title("Remove a window")
        self.resizable(False, False)
        pad = {"padx": 10, "pady": 4, "sticky": "w"}
        ttk.Label(self, text="Project").grid(row=0, column=0, **pad)
        self.proj = ttk.Combobox(self, state="readonly", width=45)
        self.proj.grid(row=0, column=1, **pad)
        self.proj.bind("<<ComboboxSelected>>", lambda e: self.fill())
        ttk.Label(self, text="Windows").grid(row=1, column=0, sticky="nw", padx=10, pady=4)
        self.lb = tk.Listbox(self, width=52, height=8, exportselection=False)
        self.lb.grid(row=1, column=1, **pad)
        b = ttk.Frame(self)
        b.grid(row=2, column=0, columnspan=2, pady=8)
        ttk.Button(b, text="Delete window", command=self.delete).pack(side="left", padx=6)
        ttk.Button(b, text="Close", command=self.destroy).pack(side="left")
        self.load()
        self.transient(app.root)
        self.grab_set()

    def load(self):
        self.data = self.app.data()
        titles = [p["title"] for p in self.data["projects"]]
        self.proj["values"] = titles
        if self.proj.get() not in titles:
            self.proj.set(titles[0] if titles else "")
        self.fill()

    def project(self):
        return next((p for p in self.data["projects"] if p["title"] == self.proj.get()), None)

    def fill(self):
        self.lb.delete(0, "end")
        p = self.project()
        self.wins = windows_of(p) if p else []
        for label, _, vs in self.wins:
            self.lb.insert("end", f"{label}   ({len(vs)} version{'' if len(vs) == 1 else 's'})")

    def delete(self):
        sel = self.lb.curselection()
        if not sel:
            return messagebox.showinfo("Remove a window", "Select a window first.", parent=self)
        label, key, vs = self.wins[sel[0]]
        p = self.project()
        msg = (f'Delete the "{label}" window and its {len(vs)} version(s)?\n\n'
               "Its STEP copies are removed from the portfolio folder. Your original exports "
               "are not touched, and git history keeps a copy if you need it back.")
        if len(self.wins) == 1:
            msg += f'\n\nThis is the project\'s only window, so the whole "{p["title"]}" project and its card are removed too.'
        if messagebox.askyesno("Delete window", msg, icon="warning", parent=self):
            if self.app.remove_window(p["title"], key, label):
                self.load()


class App:
    def __init__(self):
        try:
            self.cfg = json.loads(CONFIG.read_text("utf-8"))
        except (OSError, ValueError):
            self.cfg = {}
        r = self.root = tk.Tk()
        r.title("Portfolio Updater")
        r.geometry("600x500")
        self.v_root = tk.StringVar(value=self.cfg.get("root", ""))
        self.v_watch = tk.StringVar(value=self.cfg.get("watch", ""))
        self.v_on = tk.BooleanVar(value=False)
        self.v_pub = tk.BooleanVar(value=self.cfg.get("publish", True))
        self.seen, self.pending, self.baselined = {}, {}, False
        f = ttk.Frame(r, padding=12)
        f.pack(fill="both", expand=True)
        f.columnconfigure(1, weight=1)
        for i, (label, var) in enumerate((("Portfolio folder", self.v_root), ("Watch folder", self.v_watch))):
            ttk.Label(f, text=label).grid(row=i, column=0, sticky="w", pady=3)
            ttk.Entry(f, textvariable=var).grid(row=i, column=1, sticky="ew", padx=6)
            ttk.Button(f, text="Browse", command=lambda v=var: self.browse(v)).grid(row=i, column=2)
        ttk.Checkbutton(f, text="Watch for new exports", variable=self.v_on, command=self.toggle).grid(row=2, column=0, columnspan=2, sticky="w", pady=6)
        ttk.Checkbutton(f, text="Publish with git after saving", variable=self.v_pub).grid(row=3, column=0, columnspan=2, sticky="w")
        ttk.Button(f, text="Refresh", command=self.refresh).grid(row=2, column=2, sticky="ew")
        ttk.Button(f, text="Add a version manually", command=self.manual).grid(row=3, column=2, sticky="ew")
        ttk.Button(f, text="Remove a window", command=self.manage).grid(row=4, column=2, sticky="ew", pady=(4, 0))
        ttk.Button(f, text="Recalculate changes", command=self.recalc).grid(row=5, column=2, sticky="ew", pady=(4, 0))
        self.out = tk.Text(f, height=12, state="disabled", wrap="word")
        self.out.grid(row=6, column=0, columnspan=3, sticky="nsew", pady=(10, 0))
        f.rowconfigure(6, weight=1)
        r.protocol("WM_DELETE_WINDOW", self.close)
        r.after(500, self.autostart)

    def autostart(self):
        """Begin watching on launch if it was on last time (on by default)."""
        if not self.cfg.get("auto", True):
            return
        if not (Path(self.v_root.get()) / "index.html").exists():
            return self.log("Set the portfolio folder and watch folder, then tick 'Watch for new exports'.")
        try:
            ok = Path(self.v_watch.get()).is_dir()
        except OSError:
            ok = False
        if not ok:
            return self.log("Watch folder not available (is the network drive connected?). "
                            "Tick 'Watch for new exports' once it is.")
        self.v_on.set(True)
        self.toggle()

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
        self.cfg.update(root=self.v_root.get(), watch=self.v_watch.get(), publish=self.v_pub.get(),
                        auto=self.v_on.get())
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
        self.pending, self.baselined = {}, True
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

    def refresh(self):
        """Recheck the watch folder now. Files already saved to a project are
        compared part by part with that project's latest version, so changes
        made while the app was closed are caught too."""
        root = Path(self.v_root.get())
        if not (root / "index.html").exists():
            return messagebox.showwarning("Portfolio folder", "Choose the portfolio folder (the one containing index.html).")
        try:
            files = self.steps()
        except OSError:
            return self.log("Refresh failed: the watch folder is not available (is the network drive connected?).")
        self.log(f"Checking {len(files)} STEP file(s)...")
        self.root.update_idletasks()
        data, links, found, skipped = self.data(), self.cfg.get("links", {}), 0, 0
        for p in files:
            s = self.sig(p)
            proj = next((x for x in data["projects"] if x["title"] == links.get(str(p))), None)
            lv = latest_for(proj, p.name) if proj else None  # latest version of this file's own window
            if lv:
                try:
                    old, new = saved_parts(root, lv), part_hashes(p)
                except Exception as e:
                    self.log(f"Could not read {p.name}: {e}")
                    continue
                if old is not None and new:
                    changes, removed = diff(old, new)
                    if not changes and not removed:
                        self.seen[p] = s
                        continue
                    self.log(f"{p.name}: parts changed since the last version of {proj['title']}.")
                elif self.seen.get(p) == s:  # no named parts to compare; go by file changes
                    continue
            elif not self.baselined:
                skipped += 1  # not saved by this app and no baseline to compare against
                continue
            elif self.seen.get(p) == s:
                continue
            found += 1
            self.seen[p] = s
            self.root.deiconify(); self.root.lift()
            Prompt(self, p, proj["title"] if proj else None)
        if skipped:
            self.log(f"{skipped} other STEP file(s) skipped. Tick 'Watch for new exports' to detect new files.")
        if not found:
            self.log("Refresh: no part changes found.")

    def manual(self):
        if not self.ready():
            return
        f = filedialog.askopenfilename(filetypes=[("STEP files", "*.step *.stp")])
        if f:
            Prompt(self, Path(f))

    def save(self, path, choice, note, new, picture="", adopt=False):
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
            msg = add_version(root, proj, note, path, adopt)
            if picture:
                size = set_cover(root, proj, picture)
                msg += " Card picture saved." + (" It is large, which will slow the home page." if size > 1_500_000 else "")
            save_data(root, data)
        except Exception as e:
            messagebox.showerror("Could not save", str(e))
            return False
        self.cfg["last"] = proj["title"]
        self.cfg.setdefault("links", {})[str(path)] = proj["title"]  # lets Refresh compare this file later
        self.log(f"Saved {proj['title']}. {msg}")
        self.push(root, f"{proj['title']}: {note[:60]}")
        return True

    def push(self, root, message):
        if not self.v_pub.get():
            return
        if (Path(root) / ".git").exists():
            self.log("Publishing...")
            publish(root, message, lambda t: self.root.after(0, self.log, t))
        else:
            self.log("Not published: the portfolio folder is not a git repository.")

    def recalc(self):
        """Redo the change detection for every saved version (fixes versions saved
        before part fingerprints read Designcenter's AP242 geometry correctly)."""
        if not self.ready():
            return
        root = Path(self.v_root.get())
        self.log("Recalculating changes for all projects...")
        self.root.update_idletasks()
        try:
            data = load_data(root)
            fixed = sum(recalc_changes(root, p) for p in data["projects"])
            if fixed:
                save_data(root, data)
        except Exception as e:
            return messagebox.showerror("Could not recalculate", str(e))
        if not fixed:
            return self.log("No differences: all saved changes were already correct.")
        self.log(f"Updated the changes on {fixed} version(s).")
        self.push(root, "Recalculate part changes")

    def manage(self):
        if self.ready():
            Manage(self)

    def remove_window(self, title, key, label):
        root = Path(self.v_root.get())
        try:
            data = load_data(root)
            proj = next(p for p in data["projects"] if p["title"] == title)
            n = delete_window(root, proj, key)
            emptied = not proj["versions"]
            if emptied:
                data["projects"].remove(proj)
                d = root / "projects" / proj["id"]
                if d.is_dir() and inside(root, d):
                    shutil.rmtree(d, ignore_errors=True)
            save_data(root, data)
        except Exception as e:
            messagebox.showerror("Could not delete", str(e))
            return False
        links = self.cfg.get("links", {})
        for path in [k for k, t in links.items() if t == title and (emptied or model_key(Path(k).name) == key)]:
            links.pop(path)
        self.log(f'Deleted the "{label}" window from {title} ({n} version(s)).'
                 + (" The project had no windows left, so it was removed." if emptied else ""))
        self.push(root, f"{title}: removed {label}")
        return True


if __name__ == "__main__":
    App().run()
