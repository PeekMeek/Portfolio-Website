"""Portfolio Updater: adds a new model version to your portfolio site.

Run with Windows Python 3 (tkinter is included). No extra packages needed.
1. Set the portfolio folder (the one containing index.html) and your Projects
   folder (one subfolder per project).
2. Only files inside a project's "Website Files" folder are used for the site.
   Tick "Watch Website Files folders for new exports", or use "Add a version manually".
3. When a STEP file appears there, answer "What did you change?" and save.
The app copies the file into projects/<project>/, detects which parts changed
since the previous version, and updates data/projects.json.
"""
import hashlib, json, os, re, shutil, subprocess, threading, time
from collections import defaultdict
from datetime import date, datetime
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


# ---------- Projects folder scan ----------
STEP_EXT = (".step", ".stp")
MAX_STEP_BYTES = 95 * 1024 * 1024  # GitHub refuses files over 100 MB
SKIP = (".", "$", "~")  # hidden, system and temporary folders/files


def npath(p):
    return str(p).replace("\\", "/").rstrip("/").lower()


def iso(ts):
    return datetime.fromtimestamp(ts).isoformat(timespec="seconds")


def project_folders(projects_dir):
    return sorted((p for p in Path(projects_dir).iterdir() if p.is_dir() and not p.name.startswith(SKIP)),
                  key=lambda p: p.name.lower())


WEB_FOLDER = "website files"  # only files inside a folder with this name go on the site
IMAGE_EXT = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".avif", ".bmp")


def scan_folder(d):
    """For project folder d: the newest modified time of anything inside it (the card's
    date), plus the STEP files and pictures that sit inside a "Website Files" folder
    (at any depth, subfolders included), oldest first. Everything else is ignored."""
    newest, steps, images = Path(d).stat().st_mtime, [], []
    for dirpath, dirnames, files in os.walk(d):
        dirnames[:] = [x for x in dirnames if not x.startswith(SKIP)]
        rel = Path(dirpath).relative_to(d).parts
        for_web = any(part.lower() == WEB_FOLDER for part in rel)
        for f in files:
            if f.startswith(SKIP):
                continue
            fp = Path(dirpath, f)
            try:
                m = fp.stat().st_mtime
            except OSError:
                continue
            newest = max(newest, m)
            if for_web and fp.suffix.lower() in STEP_EXT:
                steps.append((m, fp))
            elif for_web and fp.suffix.lower() in IMAGE_EXT:
                images.append((m, fp))
    steps.sort()
    images.sort()
    return newest, steps, images


def find_web_dirs(projects_dir):
    """Every "Website Files" folder inside the project folders: [(project folder, web folder)]."""
    out = []
    for d in project_folders(projects_dir):
        for dirpath, dirnames, _ in os.walk(d):
            keep = []
            for x in dirnames:
                if x.startswith(SKIP):
                    continue
                if x.lower() == WEB_FOLDER:
                    out.append((d, Path(dirpath, x)))  # its subfolders are watched with it
                else:
                    keep.append(x)
            dirnames[:] = keep
    return out


def web_files(web_dir, exts):
    """Files with these endings inside one Website Files folder, subfolders included."""
    found = []
    for dirpath, dirnames, files in os.walk(web_dir):
        dirnames[:] = [x for x in dirnames if not x.startswith(SKIP)]
        found += [Path(dirpath, f) for f in files if not f.startswith(SKIP) and Path(f).suffix.lower() in exts]
    return found


def web_steps(web_dir):
    return web_files(web_dir, STEP_EXT)


def match_project(data, links, d):
    """The site project that belongs to folder d, if any."""
    nd = npath(d)
    for p in data["projects"]:
        if p.get("folder") and npath(p["folder"]) == nd:
            return p
    for path, title in links.items():  # a file from this folder was saved to a project
        if npath(path).startswith(nd + "/"):
            p = next((x for x in data["projects"] if x["title"] == title), None)
            if p:
                return p
    return next((p for p in data["projects"] if p["title"].lower() == Path(d).name.lower()), None)


def unique_id(data, title, skip=None):
    # old addresses (aliases) stay reserved so they keep pointing at their project;
    # skip: the project being renamed, which may take back one of its own old addresses
    others = [p for p in data["projects"] if p is not skip]
    ids = {p["id"] for p in others} | {a for p in others for a in p.get("aliases", [])}
    pid, n = slug(title), 2
    while pid in ids:
        pid = f"{slug(title)}-{n}"
        n += 1
    return pid


def import_models(root, proj, steps, imported, log):
    """Add the folder's STEP files that are new or changed since they were last imported.
    Files with the same name in different subfolders (V1, V2...) become versions of one
    window, in date order. Returns how many versions were added."""
    n, base = 0, Path(proj.get("folder", ""))
    for m, fp in steps:
        k = npath(fp)
        if imported.get(k, 0) >= m:
            continue
        try:
            big = fp.stat().st_size > MAX_STEP_BYTES
        except OSError:
            continue
        imported[k] = m
        if big:
            log(f"Skipped {fp.name}: it is over 95 MB, which GitHub won't accept.")
            continue
        try:
            rel = fp.relative_to(base).as_posix()
        except ValueError:
            rel = fp.name
        add_version(root, proj, f"Imported from {rel}", fp)
        proj["versions"][-1]["date"] = iso(m)[:10]  # the file's own date, not today's
        n += 1
    return n


def import_cover(root, proj, images, imported):
    """Use the newest picture in Website Files as the card picture, unless a picture
    was chosen by hand in the updater. Returns True if the picture changed."""
    if not images or (proj.get("image") and not proj.get("image_from_folder")):
        return False
    m, fp = images[-1]
    if imported.get(npath(fp), 0) >= m and proj.get("image"):
        return False
    imported[npath(fp)] = m
    set_cover(root, proj, fp)
    proj["image_from_folder"] = True
    return True


def change_id(root, data, proj, title):
    """Give a renamed project a page address that matches its new title (copper-pc for
    "Copper PC"). Its files in the portfolio folder move along, and the old address is
    kept as an alias so links already shared still open the project. Returns the new id."""
    old = proj["id"]
    if slug(title) == old:
        return old
    new = unique_id(data, title, skip=proj)
    if new == old:
        return old
    src, dst = Path(root) / "projects" / old, Path(root) / "projects" / new
    if src.is_dir():
        if dst.exists():
            return old  # something already uses that folder: keep the current address
        shutil.move(str(src), str(dst))
    a, b = f"projects/{old}/", f"projects/{new}/"
    fix = lambda s: b + s[len(a):] if isinstance(s, str) and s.startswith(a) else s
    for v in proj["versions"]:
        if "file" in v:
            v["file"] = fix(v["file"])
    if proj.get("image"):
        proj["image"] = fix(proj["image"])
    aliases = [x for x in proj.get("aliases", []) if x != new]
    if old not in aliases:
        aliases.append(old)
    proj["aliases"], proj["id"] = aliases, new
    return new


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
from tkinter import filedialog, messagebox, simpledialog, ttk

# ---------- System tray (needs: py -m pip install pystray pillow) ----------
try:
    import pystray
    from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageOps, ImageTk
except ImportError:  # still works as a normal window without them
    pystray = Image = ImageDraw = ImageEnhance = ImageFilter = ImageOps = ImageTk = None

INSTANCE_PORT = 47613  # lets a second launch bring the running updater forward instead


def tray_image(size=64):
    """The app icon: a red wireframe cube on the site's near-black."""
    im = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d, s = ImageDraw.Draw(im), size / 64
    d.rounded_rectangle([1 * s, 1 * s, 63 * s, 63 * s], radius=12 * s, fill="#121212")
    P = lambda x, y: (x * s, y * s)
    top, l, r, c, bl, br, b = P(32, 10), P(12, 21), P(52, 21), P(32, 32), P(12, 43), P(52, 43), P(32, 54)
    w = max(2, round(4.5 * s))
    d.line([top, r, br, b, bl, l, top], fill="#f47068", width=w, joint="curve")
    d.line([l, c, r], fill="#f47068", width=w, joint="curve")
    d.line([c, b], fill="#f47068", width=w)
    return im


class Tray:
    """The icon by the clock. Left-click opens the window; right-click shows the menu."""
    def __init__(self, app):
        self.app = app
        on_ui = lambda fn: (lambda icon, item: app.root.after(0, fn))  # menu clicks run on the window's thread
        menu = pystray.Menu(
            pystray.MenuItem("Open Portfolio Updater", on_ui(app.toggle_panel), default=True),
            pystray.MenuItem("Open website", on_ui(app.open_site)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Watch Website Files folders", on_ui(app.toggle_from_tray),
                             checked=lambda item: app.watching),
            pystray.MenuItem("Refresh", on_ui(app.refresh)),
            pystray.MenuItem("Scan projects folder", on_ui(app.scan)),
            pystray.MenuItem("Manage projects", on_ui(lambda: (app.show(), app.manage()))),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", on_ui(app.quit)))
        self.icon = pystray.Icon("portfolio_updater", tray_image(), "Portfolio Updater", menu)
        threading.Thread(target=self.icon.run, daemon=True).start()

    def tip(self, text):
        """Text shown when you hover over the icon."""
        try:
            self.icon.title = ("Portfolio Updater: " + text)[:127]
        except Exception:
            pass

    def note(self, text):
        """A Windows notification from the icon."""
        try:
            self.icon.notify(text, "Portfolio Updater")
        except Exception:
            pass

    def stop(self):
        try:
            self.icon.stop()
        except Exception:
            pass


def center_on(win, master):
    """Open a dialog centered over the updater window, kept on screen."""
    try:
        win.update_idletasks()
        w, h = win.winfo_reqwidth(), win.winfo_reqheight()
        if master.state() == "withdrawn":
            left, top, right, bottom = work_area(master)
            x, y = right - w - 30, bottom - h - 60
        else:
            x = master.winfo_rootx() + (master.winfo_width() - w) // 2
            y = master.winfo_rooty() + (master.winfo_height() - h) // 2
        left, top, right, bottom = work_area(master)
        x = min(max(left, x), right - w - 16)
        y = min(max(top, y), bottom - h - 40)
        win.geometry(f"+{x}+{y}")
    except Exception:
        pass


def work_area(root):
    """The screen area not covered by the taskbar: (left, top, right, bottom)."""
    if os.name == "nt":
        try:
            import ctypes
            import ctypes.wintypes as wt
            rect = wt.RECT()
            if ctypes.windll.user32.SystemParametersInfoW(48, 0, ctypes.byref(rect), 0):  # SPI_GETWORKAREA
                return rect.left, rect.top, rect.right, rect.bottom
        except Exception:
            pass
    return 0, 0, root.winfo_screenwidth(), root.winfo_screenheight() - 48


def site_url(root):
    """The published site's address: the CNAME domain if set, else the GitHub Pages address."""
    cname = Path(root) / "CNAME"
    if cname.is_file():
        domain = cname.read_text().strip()
        if domain:
            return f"https://{domain}/"
    try:
        r = subprocess.run(["git", "remote", "get-url", "origin"], cwd=root, capture_output=True, text=True,
                           creationflags=0x08000000 if os.name == "nt" else 0)
        m = re.search(r"github\.com[:/]([^/]+)/([^/\s]+?)(?:\.git)?\s*$", r.stdout)
        if m:
            user, repo = m.group(1).lower(), m.group(2)
            return f"https://{user}.github.io/" if repo.lower() == f"{user}.github.io" else f"https://{user}.github.io/{repo}/"
    except OSError:
        pass
    return None


def already_running():
    """If the updater is already open, tell it to show its window and return True."""
    import socket
    try:
        with socket.create_connection(("127.0.0.1", INSTANCE_PORT), timeout=0.5) as s:
            s.sendall(b"show")
        return True
    except OSError:
        return False


def listen_for_second_launch(app):
    import socket
    srv = socket.socket()
    try:
        srv.bind(("127.0.0.1", INSTANCE_PORT))
        srv.listen(1)
    except OSError:
        return  # the port is taken by something else: just skip this nicety
    def run():
        while True:
            try:
                conn, _ = srv.accept()
                conn.close()
                app.root.after(0, app.show)
            except OSError:
                return
    threading.Thread(target=run, daemon=True).start()


# ---------- One change to projects.json at a time ----------
# Every load-change-save of the project list holds this lock, so a background task
# (a scan, a picture update) can't write an older copy over a change you just saved.
DATA_LOCK = threading.RLock()


def locked(fn):
    """For background tasks: wait for the lock, then run."""
    def run(*a, **k):
        with DATA_LOCK:
            return fn(*a, **k)
    return run


def exclusive(method):
    """For buttons: if a background task is mid-save, say so instead of freezing the window."""
    def run(self, *a, **k):
        if not DATA_LOCK.acquire(blocking=False):
            messagebox.showinfo("One moment", "The updater is still saving another change "
                                "(see the log). Try again in a few seconds.")
            return False
        try:
            return method(self, *a, **k)
        finally:
            DATA_LOCK.release()
    return run


# ---------- Dark theme (same colors as the website) ----------
BG, PANEL, FIELD, LINE = "#121212", "#1b1b1b", "#161616", "#2c2c2c"
INK, MUTE, ACCENT, ACCENT_DEEP = "#efe8e6", "#a69e9c", "#f47068", "#c93b37"


def apply_theme(root):
    """Dark window colors for every window the app opens."""
    # classic Tk widgets (text boxes, lists, the rename dialog) read these defaults
    for k, v in {"*background": BG, "*foreground": INK, "*Text.background": FIELD, "*Text.insertBackground": INK,
                 "*Text.selectBackground": ACCENT_DEEP, "*Text.selectForeground": "#ffffff",
                 "*Listbox.background": FIELD, "*Listbox.selectBackground": ACCENT_DEEP,
                 "*Listbox.selectForeground": "#ffffff", "*Entry.background": FIELD, "*Entry.insertBackground": INK,
                 "*highlightBackground": LINE, "*highlightColor": ACCENT, "*highlightThickness": 1,
                 "*relief": "flat", "*borderWidth": 0, "*activeBackground": "#262626", "*activeForeground": INK,
                 "*Button.padX": 10, "*Button.padY": 3,
                 "*TCombobox*Listbox.background": FIELD, "*TCombobox*Listbox.foreground": INK,
                 "*TCombobox*Listbox.selectBackground": ACCENT_DEEP}.items():
        root.option_add(k, v)
    root.configure(background=BG)
    s = ttk.Style(root)
    s.theme_use("clam")  # the built-in theme that accepts custom colors
    s.configure(".", background=BG, foreground=INK, fieldbackground=FIELD, bordercolor=LINE,
                lightcolor=BG, darkcolor=BG, troughcolor=BG, focuscolor=ACCENT,
                selectbackground=ACCENT_DEEP, selectforeground="#ffffff", insertcolor=INK, arrowcolor=INK,
                font=("Segoe UI", 9))
    s.configure("Hint.TLabel", foreground=MUTE)
    s.configure("TButton", background=PANEL, foreground=INK, bordercolor=LINE, padding=(10, 4))
    s.map("TButton", background=[("pressed", ACCENT_DEEP), ("active", "#262626")],
          bordercolor=[("focus", ACCENT), ("active", "#3a3a3a")])
    s.configure("Accent.TButton", background=ACCENT_DEEP, foreground="#ffffff", bordercolor=ACCENT_DEEP)
    s.map("Accent.TButton", background=[("pressed", "#922323"), ("active", ACCENT)],
          foreground=[("active", "#121212")])
    s.configure("TCheckbutton", background=BG, foreground=INK, indicatorbackground=FIELD,
                indicatorforeground="#ffffff", indicatormargin=(0, 0, 6, 0))
    s.map("TCheckbutton", indicatorbackground=[("selected", ACCENT_DEEP), ("active", "#262626")],
          background=[("active", BG)])
    s.configure("TEntry", fieldbackground=FIELD, foreground=INK, bordercolor=LINE, insertcolor=INK)
    s.map("TEntry", fieldbackground=[("disabled", BG)], foreground=[("disabled", MUTE)],
          bordercolor=[("focus", ACCENT)])
    s.configure("TCombobox", fieldbackground=FIELD, background=PANEL, foreground=INK, arrowcolor=INK)
    s.map("TCombobox", fieldbackground=[("readonly", FIELD)], foreground=[("readonly", INK)],
          bordercolor=[("focus", ACCENT)])
    s.configure("Vertical.TScrollbar", background=PANEL, troughcolor=BG, bordercolor=BG, arrowcolor=MUTE)
    s.map("Vertical.TScrollbar", background=[("active", "#333333")])
    s.configure("TSeparator", background=LINE)
    s.configure("Card.Vertical.TScrollbar", background="#2a2a2a", troughcolor="#1b1b1b", bordercolor="#1b1b1b",
                arrowcolor=MUTE, lightcolor="#2a2a2a", darkcolor="#2a2a2a")
    s.map("Card.Vertical.TScrollbar", background=[("active", "#383838")])


def round_corners(win):
    """Ask Windows 11 to round the window's corners (ignored on Windows 10 and elsewhere)."""
    if os.name != "nt":
        return
    try:
        import ctypes
        win.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(win.winfo_id())
        pref = ctypes.c_int(2)  # DWMWCP_ROUND
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(pref), ctypes.sizeof(pref))
    except Exception:
        pass


def dark_titlebar(win):
    """Dark title bar on Windows 10/11, with the red accent as the window border on Windows 11.
    Does nothing elsewhere or on older Windows versions."""
    if os.name != "nt":
        return
    try:
        import ctypes
        win.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(win.winfo_id())
        set_attr = ctypes.windll.dwmapi.DwmSetWindowAttribute
        on = ctypes.c_int(1)
        for attr in (20, 19):  # "use dark mode": 20 on current Windows, 19 on older Windows 10 builds
            if set_attr(hwnd, attr, ctypes.byref(on), ctypes.sizeof(on)) == 0:
                break
        bgr = lambda h: ctypes.c_int(int(h[5:7] + h[3:5] + h[1:3], 16))  # COLORREF is blue-green-red
        for attr, color in ((35, BG), (34, ACCENT_DEEP)):  # Windows 11: caption color, border color
            c = bgr(color)
            set_attr(hwnd, attr, ctypes.byref(c), ctypes.sizeof(c))
    except Exception:
        pass


# ---------- Panel pieces (the tray panel's look, after Bambu Bar) ----------
import tkinter.font as tkfont

CARD, TILE, BTN, BTN_HI = "#1b1b1b", "#232323", "#232323", "#2e2e2e"
GREEN, AMBER = "#3fb97a", "#e8b020"
GREEN_TINT, AMBER_TINT, RED_TINT, RED_TINT_HI = "#16301f", "#33290f", "#3a1d1c", "#4c2523"
PANEL_W = 392
# Windows' own icon font (Segoe Fluent Icons on Windows 11, Segoe MDL2 Assets on Windows 10)
ICONS = {"refresh": "", "settings": "", "hide": "", "add": "", "list": "",
         "search": "", "open": "", "globe": "", "hidden": "", "history": ""}
PLAIN = {"refresh": "↻", "settings": "⚙", "hide": "—", "add": "+", "list": "☰",
         "search": "⌕", "open": "↗", "globe": "○", "hidden": "◌", "history": "◷"}
ICONS.update(left="", right="", check="", delete="")
PLAIN.update(left="‹", right="›", check="✓", delete="✕")


def icon_family(root):
    fams = set(tkfont.families(root))
    return next((f for f in ("Segoe Fluent Icons", "Segoe MDL2 Assets") if f in fams), None)


def plain(widget, **kw):
    """Classic Tk widgets pick up a 1px outline from the theme; panel pieces don't want it."""
    widget.config(bd=0, highlightthickness=0, **kw)
    return widget


def rounded_photo(w, h, radius, fill, outline=None, scale=3):
    """A smooth rounded rectangle (drawn 3x larger, then scaled down so the curves are clean)."""
    im = Image.new("RGBA", (w * scale, h * scale), (0, 0, 0, 0))
    ImageDraw.Draw(im).rounded_rectangle([0, 0, w * scale - 1, h * scale - 1], radius=radius * scale, fill=fill,
                                         outline=outline, width=scale if outline else 0)
    return ImageTk.PhotoImage(im.resize((w, h), Image.LANCZOS))


def thumb_photo(path, w=104, h=78, radius=10):
    """A project's card picture, whole, over a blurred copy of itself, with rounded corners."""
    s = 2
    im = Image.open(path).convert("RGB")
    back = ImageEnhance.Brightness(ImageOps.fit(im, (w * s, h * s)).filter(ImageFilter.GaussianBlur(10))).enhance(.45)
    im.thumbnail((w * s, h * s), Image.LANCZOS)
    back.paste(im, ((w * s - im.width) // 2, (h * s - im.height) // 2))
    mask = Image.new("L", (w * s, h * s), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, w * s - 1, h * s - 1], radius=radius * s, fill=255)
    out = Image.new("RGBA", (w * s, h * s), (0, 0, 0, 0))
    out.paste(back, (0, 0), mask)
    return ImageTk.PhotoImage(out.resize((w, h), Image.LANCZOS))


def placeholder_photo(w=104, h=78, radius=10):
    """A dim wireframe cube, for projects without a picture."""
    s = 3
    im = Image.new("RGBA", (w * s, h * s), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle([0, 0, w * s - 1, h * s - 1], radius=radius * s, fill=TILE)
    cx, cy, k = w * s / 2, h * s / 2, h * s / 5
    P = lambda x, y: (cx + x * k, cy + y * k)
    top, l, r_, c, bl, br, b = P(0, -1.1), P(-1, -.55), P(1, -.55), P(0, 0), P(-1, .65), P(1, .65), P(0, 1.2)
    line = dict(fill="#3d3d3d", width=2 * s)
    d.line([top, r_, br, b, bl, l, top], joint="curve", **line)
    d.line([l, c, r_], joint="curve", **line)
    d.line([c, b], **line)
    return ImageTk.PhotoImage(im.resize((w, h), Image.LANCZOS))


class Card(tk.Frame):
    """A rounded box. Put content in .body, whose background is the card color."""
    def __init__(self, parent, color=CARD, radius=14, padx=14, pady=12, outline=None):
        under = parent.cget("bg")
        super().__init__(parent, bg=under, bd=0, highlightthickness=0)
        self.color, self.radius, self.outline, self._drawn = color, radius, outline, None
        self._bg = plain(tk.Label(self, bg=under))
        self._bg.place(x=0, y=0, relwidth=1, relheight=1)
        self.body = plain(tk.Frame(self, bg=color))
        self.body.pack(fill="both", expand=True, padx=padx, pady=pady)
        if Image is None:  # no Pillow: square corners
            self.config(bg=color)
            self._bg.config(bg=color)
        else:
            self.bind("<Configure>", self._draw)

    def _draw(self, _=None):
        w, h = self.winfo_width(), self.winfo_height()
        if w < 4 or h < 4 or (w, h, self.color) == self._drawn:
            return
        self._drawn = (w, h, self.color)
        self._img = rounded_photo(w, h, self.radius, self.color, self.outline)
        self._bg.config(image=self._img)

    def recolor(self, color):
        self.color = color
        stack = [self.body]
        while stack:  # the body and everything inside it
            w = stack.pop()
            try:
                w.config(bg=color)
            except tk.TclError:
                pass
            stack += w.winfo_children()
        if Image is None:
            self.config(bg=color)
            self._bg.config(bg=color)
        else:
            self._draw()


class Pill(Card):
    """A small rounded status label with a dot, like Bambu Bar's "Printing"."""
    def __init__(self, parent, text="", fg=MUTE, color=TILE):
        super().__init__(parent, color=color, radius=11, padx=10, pady=3)
        self.dot = plain(tk.Label(self.body, text="●", fg=fg, bg=color, font=("Segoe UI", 7)))
        self.dot.pack(side="left", padx=(0, 6))
        self.lbl = plain(tk.Label(self.body, text=text, fg=fg, bg=color, font=("Segoe UI Semibold", 9)))
        self.lbl.pack(side="left")

    def set(self, text, fg, color):
        if (text, fg, color) == getattr(self, "_state", None):
            return
        self._state = (text, fg, color)
        self.recolor(color)
        self.dot.config(fg=fg)
        self.lbl.config(text=text, fg=fg)


class RoundButton(Card):
    """A rounded button with an optional icon, lighter while the pointer is over it."""
    def __init__(self, parent, text, command, icon=None, ifam=None, color=BTN, hover=BTN_HI, fg=INK,
                 radius=10, padx=12, pady=8, size=10, tip=None):
        super().__init__(parent, color=color, radius=radius, padx=padx, pady=pady)
        self.base, self.hover, self.command = color, hover, command
        inner = plain(tk.Frame(self.body, bg=color))
        inner.pack(anchor="center")
        if icon:
            glyph, font = (ICONS[icon], (ifam, size)) if ifam else (PLAIN[icon], ("Segoe UI Symbol", size))
            plain(tk.Label(inner, text=glyph, font=font, fg=fg, bg=color)).pack(side="left", padx=(0, 7 if text else 0))
        if text:
            plain(tk.Label(inner, text=text, font=("Segoe UI Semibold", size), fg=fg, bg=color)).pack(side="left")
        for w in (self, self._bg, self.body, inner, *inner.winfo_children()):
            w.bind("<Enter>", lambda e: self.recolor(self.hover))
            w.bind("<Leave>", self._leave)
            w.bind("<Button-1>", lambda e: self.command())
            w.config(cursor="hand2")

    def _leave(self, e):
        w = self.winfo_containing(e.x_root, e.y_root)
        while w is not None and w is not self:
            w = w.master
        if w is None:  # really left, not just moved onto the label inside
            self.recolor(self.base)


class Link(tk.Label):
    """Text that acts as a button, like Bambu Bar's "Open Bambu Bar" and "Quit"."""
    def __init__(self, parent, text, command, fg=MUTE, hover=INK, **kw):
        super().__init__(parent, text=text, fg=fg, bg=parent.cget("bg"), cursor="hand2",
                         font=kw.pop("font", ("Segoe UI Semibold", 10)), **kw)
        plain(self)
        self.bind("<Enter>", lambda e: self.config(fg=hover))
        self.bind("<Leave>", lambda e: self.config(fg=fg))
        self.bind("<Button-1>", lambda e: command())


class SettingsDialog(tk.Toplevel):
    """Folders, watching and publishing, and the tools you rarely need."""
    def __init__(self, app):
        super().__init__(app.root)
        self.app = app
        self.title("Settings")
        self.resizable(False, False)
        f = ttk.Frame(self, padding=14)
        f.pack(fill="both", expand=True)
        f.columnconfigure(1, weight=1)
        for i, (label, var) in enumerate((("Portfolio folder", app.v_root), ("Projects folder", app.v_projects))):
            ttk.Label(f, text=label).grid(row=i, column=0, sticky="w", pady=4)
            ttk.Entry(f, textvariable=var, width=44).grid(row=i, column=1, sticky="ew", padx=8)
            ttk.Button(f, text="Browse", command=lambda v=var: app.browse(v)).grid(row=i, column=2)
        ttk.Checkbutton(f, text="Watch Website Files folders for new exports", variable=app.v_on,
                        command=app.toggle).grid(row=2, column=0, columnspan=3, sticky="w", pady=(12, 4))
        ttk.Checkbutton(f, text="Publish with git after saving", variable=app.v_pub).grid(
            row=3, column=0, columnspan=3, sticky="w")
        ttk.Separator(f).grid(row=4, column=0, columnspan=3, sticky="ew", pady=12)
        tools = ttk.Frame(f)
        tools.grid(row=5, column=0, columnspan=3, sticky="ew")
        ttk.Button(tools, text="Recalculate changes", command=app.recalc).pack(side="left")
        ttk.Button(tools, text="Full log", command=app.show_log).pack(side="left", padx=6)
        ttk.Button(tools, text="Done", style="Accent.TButton", command=self.done).pack(side="right")
        self.protocol("WM_DELETE_WINDOW", self.done)
        dark_titlebar(self)
        center_on(self, app.root)
        self.transient(app.root)

    def done(self):
        self.app.save_cfg()
        self.destroy()
        self.app.refresh_panel()


class LogDialog(tk.Toplevel):
    """Everything the updater has done since it started."""
    def __init__(self, app):
        super().__init__(app.root)
        self.title("Portfolio Updater log")
        self.geometry("640x420")
        box = ttk.Frame(self, padding=10)
        box.pack(fill="both", expand=True)
        self.text = tk.Text(box, wrap="word", font=("Consolas", 9), padx=8, pady=6)
        sb = ttk.Scrollbar(box, orient="vertical", command=self.text.yview)
        self.text.config(yscrollcommand=sb.set)
        self.text.pack(side="left", fill="both", expand=True)
        sb.pack(side="left", fill="y")
        for when, line in app.log_lines:
            self.add(when, line)
        dark_titlebar(self)
        center_on(self, app.root)

    def add(self, when, line):
        self.text.config(state="normal")
        self.text.insert("end", f"{when:%I:%M:%S %p}  {line}\n")
        self.text.see("end")
        self.text.config(state="disabled")


class Prompt(tk.Toplevel):
    def __init__(self, app, path, project=None, folder=None):
        super().__init__(app.root)
        self.app, self.path, self.folder = app, path, folder
        self.title("What did you change?")
        self.resizable(False, False)
        pad = {"padx": 10, "pady": 4, "sticky": "w"}
        ttk.Label(self, text=f"New export: {path.name}", font=("Segoe UI", 10, "bold")).grid(row=0, column=0, columnspan=2, **pad)
        names = [p["title"] for p in app.data()["projects"]]
        ttk.Label(self, text="Project").grid(row=1, column=0, **pad)
        self.proj = ttk.Combobox(self, values=names + [NEW], state="readonly", width=38)
        # From a project folder with no card yet: start a new project named after the folder.
        last = project or (None if folder else app.cfg.get("last"))
        self.proj.set(last if last in names else (NEW if folder or not names else names[0]))
        self.proj.grid(row=1, column=1, **pad)
        self.proj.bind("<<ComboboxSelected>>", lambda e: self.refresh())
        self.f = {}
        for i, k in enumerate(("Title", "Summary", "Tags")):
            ttk.Label(self, text=k + (" (comma separated)" if k == "Tags" else "")).grid(row=2 + i, column=0, **pad)
            self.f[k] = ttk.Entry(self, width=41)
            self.f[k].grid(row=2 + i, column=1, **pad)
        if folder and self.proj.get() == NEW:
            self.f["Title"].insert(0, Path(folder).name)
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
        ttk.Button(b, text="Save version", style="Accent.TButton", command=self.ok).pack(side="left", padx=6)
        ttk.Button(b, text="Skip", command=self.destroy).pack(side="left")
        self.refresh()
        dark_titlebar(self)
        center_on(self, app.root)
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
        if self.app.save(self.path, self.proj.get(), note, fields, self.pic.get(), adopt, self.folder):
            self.destroy()


def listbox(parent, width, height, mode="browse"):
    """A dark list with its own scrollbar, in a frame the color of whatever it sits on.
    Returns (frame, listbox)."""
    under = parent.cget("bg") if isinstance(parent, (tk.Frame, tk.Toplevel, tk.Tk)) else BG
    box = plain(tk.Frame(parent, bg=under))
    lb = tk.Listbox(box, width=width, height=height, selectmode=mode, exportselection=False, activestyle="none",
                    font=("Segoe UI", 10), bg=FIELD, fg=INK, selectbackground=ACCENT_DEEP, selectforeground="#ffffff",
                    highlightthickness=1, highlightbackground=LINE, highlightcolor=LINE, bd=0, relief="flat")
    sb = ttk.Scrollbar(box, orient="vertical", command=lb.yview, style="Card.Vertical.TScrollbar")
    lb.config(yscrollcommand=sb.set)
    lb.pack(side="left", fill="both", expand=True)
    sb.pack(side="left", fill="y", padx=(2, 0))
    return box, lb


class Manage(tk.Toplevel):
    """Show/hide projects, edit a project's title, description and tags, delete windows or
    projects. Drawn in the same style as the tray panel: rounded cards on near-black."""
    def __init__(self, app):
        super().__init__(app.root)
        self.app = app
        self.current = app.cfg.get("last")  # the project whose details are shown below
        self.title("Manage projects")
        self.resizable(False, False)
        self.configure(bg=BG)
        fam = icon_family(self)
        L = lambda parent, **kw: plain(tk.Label(parent, bg=parent.cget("bg"), **{"anchor": "w", **kw}))
        head = lambda parent, text: L(parent, text=text, fg=MUTE, font=("Segoe UI Semibold", 9))
        hint = lambda parent, text="": L(parent, text=text, fg=MUTE, font=("Segoe UI", 8))
        btn = lambda parent, text, cmd, icon=None, **kw: RoundButton(parent, text, cmd, icon=icon, ifam=fam, **kw)
        row = lambda parent: plain(tk.Frame(parent, bg=parent.cget("bg")))
        outer = plain(tk.Frame(self, bg=BG))
        outer.pack(fill="both", expand=True, padx=16, pady=(14, 14))

        # ---- header ----
        hdr = row(outer)
        hdr.pack(fill="x", pady=(0, 12))
        if Image is not None:
            self._logo = ImageTk.PhotoImage(tray_image(28))
            L(hdr, image=self._logo).pack(side="left", padx=(0, 10))
        L(hdr, text="Manage projects", fg=INK, font=("Segoe UI Semibold", 13)).pack(side="left")

        # ---- card: on the website / hidden, with arrows between ----
        card = Card(outer, radius=16, padx=14, pady=12)
        card.pack(fill="x", pady=(0, 10))
        b = card.body
        head(b, "On the website").grid(row=0, column=0, sticky="w", pady=(0, 4))
        head(b, "Hidden").grid(row=0, column=2, sticky="w", pady=(0, 4))
        box, self.show_lb = listbox(b, 32, 9, "extended")
        box.grid(row=1, column=0, sticky="nsew")
        arrows = row(b)
        arrows.grid(row=1, column=1, padx=12)
        btn(arrows, "", lambda: self.move(True), icon="left", radius=9, padx=12, pady=7).pack(pady=(0, 10))
        btn(arrows, "", lambda: self.move(False), icon="right", radius=9, padx=12, pady=7).pack()
        box, self.hide_lb = listbox(b, 32, 9, "extended")
        box.grid(row=1, column=2, sticky="nsew")
        hint(b, "Click a project to edit it below. Ctrl or Shift selects several; the arrows move them "
                "between the lists.").grid(row=2, column=0, columnspan=3, sticky="w", pady=(8, 0))
        for lb, other in ((self.show_lb, self.hide_lb), (self.hide_lb, self.show_lb)):
            lb.bind("<<ListboxSelect>>", lambda e, lb=lb, other=other: self.picked(lb, other))
        self.show_lb.bind("<Double-Button-1>", lambda e: self.move(False))
        self.hide_lb.bind("<Double-Button-1>", lambda e: self.move(True))

        # ---- card: the selected project ----
        card = Card(outer, radius=16, padx=14, pady=12)
        card.pack(fill="x", pady=(0, 10))
        b = card.body
        b.columnconfigure(1, weight=1)
        head(b, "Title").grid(row=0, column=0, sticky="nw", pady=(6, 0), padx=(0, 14))
        tt = row(b)
        tt.grid(row=0, column=1, sticky="ew")
        line = row(tt)
        line.pack(fill="x")
        self.title_in = ttk.Entry(line, width=36, font=("Segoe UI Semibold", 12))
        self.title_in.pack(side="left")
        self.state_pill = Pill(line)  # On the website / Hidden
        self.state_pill.pack(side="left", padx=(10, 0))
        self.folder_note = hint(tt)
        self.folder_note.pack(anchor="w", pady=(4, 0))

        head(b, "Description").grid(row=1, column=0, sticky="nw", pady=(14, 0))
        dd = row(b)
        dd.grid(row=1, column=1, sticky="ew", pady=(12, 0))
        self.desc = tk.Text(dd, width=60, height=3, wrap="word", font=("Segoe UI", 10), padx=8, pady=6,
                            bg=FIELD, fg=INK, insertbackground=INK, highlightthickness=1,
                            highlightbackground=LINE, highlightcolor=ACCENT, bd=0, relief="flat")
        self.desc.pack(fill="x")
        hint(dd, "Shown under the title on the project's card and its page.").pack(anchor="w", pady=(4, 0))

        head(b, "Tags").grid(row=2, column=0, sticky="nw", pady=(14, 0))
        tf = row(b)
        tf.grid(row=2, column=1, sticky="w", pady=(12, 0))
        hint(tf, "This project").grid(row=0, column=0, sticky="w", pady=(0, 3))
        hint(tf, "All tags").grid(row=0, column=2, sticky="w", pady=(0, 3))
        box, self.tags = listbox(tf, 22, 5, "extended")
        box.grid(row=1, column=0)
        mid = row(tf)
        mid.grid(row=1, column=1, padx=10)
        btn(mid, "", self.add_tags, icon="left", radius=9, padx=10, pady=6).pack(pady=(0, 8))
        btn(mid, "", self.remove_tags, icon="right", radius=9, padx=10, pady=6).pack()
        box, self.all_tags = listbox(tf, 22, 5, "extended")
        box.grid(row=1, column=2)
        self.all_tags.bind("<Double-Button-1>", lambda e: self.add_tags())
        self.tags.bind("<Double-Button-1>", lambda e: self.remove_tags())
        links = row(tf)
        links.grid(row=2, column=2, sticky="w", pady=(6, 0))
        Link(links, "Rename or merge", self.rename_tag, font=("Segoe UI", 9)).pack(side="left")
        Link(links, "Delete everywhere", self.delete_tag, hover=ACCENT, font=("Segoe UI", 9)).pack(side="left", padx=(12, 0))
        new = row(tf)
        new.grid(row=3, column=0, columnspan=3, sticky="w", pady=(10, 0))
        self.tag_in = ttk.Entry(new, width=26)
        self.tag_in.pack(side="left", ipady=3)
        self.tag_in.bind("<Return>", lambda e: self.new_tag())
        btn(new, "New tag", self.new_tag, icon="add", radius=9, padx=10, pady=5, size=9).pack(side="left", padx=(8, 0))
        hint(tf, "Tags appear on the project's card on the home page.").grid(
            row=4, column=0, columnspan=3, sticky="w", pady=(6, 0))

        save = row(b)
        save.grid(row=3, column=0, columnspan=2, sticky="e", pady=(14, 0))
        btn(save, "Save changes", self.save_details, icon="check", color=ACCENT_DEEP, hover=ACCENT, fg="#ffffff").pack()

        # ---- card: 3D model windows ----
        card = Card(outer, radius=16, padx=14, pady=12)
        card.pack(fill="x", pady=(0, 12))
        b = card.body
        b.columnconfigure(0, weight=1)
        head(b, "3D model windows").grid(row=0, column=0, sticky="w", pady=(0, 4))
        box, self.lb = listbox(b, 60, 4)
        box.grid(row=1, column=0, sticky="ew")
        btn(b, "Delete window", self.delete, icon="delete", color=RED_TINT, hover=RED_TINT_HI, fg=ACCENT,
            size=9, padx=10, pady=6).grid(row=1, column=1, sticky="n", padx=(12, 0))

        # ---- footer ----
        foot = row(outer)
        foot.pack(fill="x")
        btn(foot, "Delete project", self.delete_project, icon="delete", color=RED_TINT, hover=RED_TINT_HI,
            fg=ACCENT).pack(side="left")
        btn(foot, "Close", self.close).pack(side="right")

        self.protocol("WM_DELETE_WINDOW", self.close)
        self.bind("<Escape>", lambda e: self.close())
        self.load()
        dark_titlebar(self)
        center_on(self, app.root)
        self.transient(app.root)
        self.grab_set()

    # ---- Show / Hide ----
    def picked(self, lb, other):
        sel = lb.curselection()
        if not sel:
            return
        other.selection_clear(0, "end")
        title = lb.get(sel[-1])
        if title != self.current:
            if not self.keep_edits():
                return self.select_current()
            self.current = title
            self.fill()

    def move(self, show):
        src = self.hide_lb if show else self.show_lb
        titles = [src.get(i) for i in src.curselection()]
        if not titles:
            return messagebox.showinfo("Move projects", "Select one or more projects in the "
                                       + ("Hidden" if show else "Show") + " box first.", parent=self)
        if not self.keep_edits():
            return
        def after():
            if self.winfo_exists():
                self.load()
        self.app.set_visible(titles, show, after)

    # ---- Tags ----
    def project_tags(self):
        return list(self.tags.get(0, "end"))

    def refresh_all_tags(self):
        """Every tag used by any project (or created here), minus this project's own."""
        mine = {t.lower() for t in self.project_tags()}
        pool = {}
        for p in self.data["projects"]:
            for t in p.get("tags") or []:
                pool.setdefault(t.lower(), t)
        for t in self.created:
            pool.setdefault(t.lower(), t)
        self.all_tags.delete(0, "end")
        for k in sorted(pool):
            if k not in mine:
                self.all_tags.insert("end", pool[k])

    def add_tags(self):
        picked = [self.all_tags.get(i) for i in self.all_tags.curselection()]
        if not picked:
            return messagebox.showinfo("Add tags", "Select one or more tags in the All tags list first.", parent=self)
        for t in picked:
            self.tags.insert("end", t)
        self.refresh_all_tags()

    def remove_tags(self):
        sel = list(self.tags.curselection())
        if not sel:
            return messagebox.showinfo("Remove tags", "Select one or more of this project's tags first.", parent=self)
        for i in reversed(sel):
            self.tags.delete(i)
        self.refresh_all_tags()

    def new_tag(self):
        have = {t.lower() for t in self.project_tags()}
        for t in self.tag_in.get().split(","):  # "Bracket, PETG" makes two tags
            t = t.strip()
            if t and t.lower() not in have:
                self.tags.insert("end", t)
                self.created.append(t)
                have.add(t.lower())
        self.tag_in.delete(0, "end")
        self.refresh_all_tags()

    def chosen_tag(self):
        """The tag selected in All tags, or else in this project's tags."""
        for lb in (self.all_tags, self.tags):
            sel = lb.curselection()
            if sel:
                return lb.get(sel[0])
        messagebox.showinfo("Tags", "Select a tag in either tag list first.", parent=self)
        return None

    def users_of(self, tag):
        return [p["title"] for p in self.data["projects"] if tag.lower() in {t.lower() for t in p.get("tags") or []}]

    def rename_tag(self):
        old = self.chosen_tag()
        if not old or not self.keep_edits():
            return
        new = simpledialog.askstring(
            "Rename or merge tag",
            f'Rename "{old}" to:\n\n(Type the name of an existing tag, like "3D Printed", to merge the two.)',
            initialvalue=old, parent=self)
        new = (new or "").strip()
        if not new or new == old:
            return
        if self.app.edit_tag(old, new):
            self.load()

    def delete_tag(self):
        tag = self.chosen_tag()
        if not tag or not self.keep_edits():
            return
        users = self.users_of(tag)
        if users and not messagebox.askyesno(
                "Delete tag", f'Remove the tag "{tag}" from {len(users)} project(s)?\n\n' + ", ".join(users),
                icon="warning", default="no", parent=self):
            return
        if not users:  # only created here and never saved
            self.created = [t for t in self.created if t.lower() != tag.lower()]
            return self.refresh_all_tags()
        if self.app.edit_tag(tag, None):
            self.load()

    def edits(self):
        """(title, description, tags) as they stand in the boxes."""
        return self.title_in.get().strip(), self.desc.get("1.0", "end").strip(), self.project_tags()

    def dirty(self):
        p = self.project()
        if not p:
            return False
        title, text, tags = self.edits()
        return (title != p["title"] or text != (p.get("summary") or "").strip()
                or tags != list(p.get("tags") or []) or bool(self.tag_in.get().strip()))

    def keep_edits(self):
        """Before leaving a project with unsaved changes, offer to save them.
        Returns False if the person chose to stay."""
        if not self.dirty():
            return True
        ans = messagebox.askyesnocancel("Unsaved changes",
                                        f"Save the changes to the title, description and tags of {self.current}?", parent=self)
        if ans is None:
            return False
        if ans:
            return self.save_details(reload=False) is not False
        return True

    def save_details(self, reload=True):
        p = self.project()
        if not p:
            return
        if self.tag_in.get().strip():
            self.new_tag()  # a tag typed but not yet added still counts
        title, text, tags = self.edits()
        if not title:
            messagebox.showwarning("Title", "The title can't be empty.", parent=self)
            return False
        if title.lower() != p["title"].lower() and any(
                x["title"].lower() == title.lower() for x in self.data["projects"]):
            messagebox.showwarning("Title", f'Another project is already called "{title}". Choose a different title.', parent=self)
            return False
        if (title == p["title"] and text == (p.get("summary") or "").strip()
                and tags == list(p.get("tags") or []) and slug(title) == p["id"]):
            return self.app.log(f"No changes to {p['title']}.")
        if not self.app.set_details(p["title"], text, tags, title):
            return False
        self.current = title
        if reload:
            self.load()

    def close(self):
        if self.keep_edits():
            self.destroy()

    def delete_project(self):
        p = self.project()
        if not p:
            return
        n = len(p["versions"])
        msg = (f'Delete the whole "{p["title"]}" project?\n\n'
               f"Its card, all {len(self.wins)} window(s) and {n} version(s), and its card picture are "
               "removed from the site and the portfolio folder. Your original exports are not "
               "touched, and git history keeps a copy if you need it back.")
        if messagebox.askyesno("Delete project", msg, icon="warning", default="no", parent=self):
            if self.app.remove_project(p["title"]):
                self.load()

    def load(self):
        self.data = self.app.data()
        self.created = []
        key = lambda p: p["title"].lower()
        shown = sorted((p for p in self.data["projects"] if not p.get("hidden")), key=key)
        hidden = sorted((p for p in self.data["projects"] if p.get("hidden")), key=key)
        for lb, ps in ((self.show_lb, shown), (self.hide_lb, hidden)):
            lb.delete(0, "end")
            for p in ps:
                lb.insert("end", p["title"])
        titles = [p["title"] for p in shown + hidden]
        if self.current not in titles:
            self.current = titles[0] if titles else None
        self.select_current()
        self.fill()

    def select_current(self):
        """Highlight the project being edited in whichever box it's in."""
        for lb in (self.show_lb, self.hide_lb):
            lb.selection_clear(0, "end")
            items = lb.get(0, "end")
            if self.current in items:
                i = items.index(self.current)
                lb.selection_set(i)
                lb.see(i)

    def project(self):
        return next((p for p in self.data["projects"] if p["title"] == self.current), None)

    def fill(self):
        self.lb.delete(0, "end")
        p = self.project()
        self.desc.delete("1.0", "end")
        self.tags.delete(0, "end")
        self.tag_in.delete(0, "end")
        self.title_in.delete(0, "end")
        state = "on the website" if p and not p.get("hidden") else "hidden"
        if p:
            self.state_pill.set(*(("On the website", GREEN, GREEN_TINT) if not p.get("hidden") else ("Hidden", MUTE, TILE)))
        else:
            self.state_pill.set("No projects yet", MUTE, TILE)
        folder = Path(p["folder"]).name if p and p.get("folder") else None
        self.folder_note.config(text=f'Folder: {folder}  (the folder keeps its name; only the website shows the title)'
                                if folder else "Shown as the project's name on its card and its page.")
        if p:
            self.title_in.insert(0, p["title"])
            self.desc.insert("1.0", p.get("summary") or "")
            for t in p.get("tags") or []:
                self.tags.insert("end", t)
        self.refresh_all_tags()
        self.wins = windows_of(p) if p else []
        for label, _, vs in self.wins:
            self.lb.insert("end", f"{label}   ({len(vs)} version{'' if len(vs) == 1 else 's'})")
        if p and not self.wins:
            self.lb.insert("end", "(no 3D models yet)")

    def delete(self):
        sel = self.lb.curselection()
        if not sel:
            return messagebox.showinfo("Delete window", "Select a window first.", parent=self)
        if sel[0] >= len(self.wins):
            return
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
        apply_theme(r)
        r.title("Portfolio Updater")
        self.v_root = tk.StringVar(value=self.cfg.get("root", ""))
        self.v_projects = tk.StringVar(value=self.cfg.get("projects", ""))
        self.v_on = tk.BooleanVar(value=False)
        self.v_pub = tk.BooleanVar(value=self.cfg.get("publish", True))
        self.seen, self.pending, self.baselined, self.busy = {}, {}, False, False
        self.web_dirs, self.watch_from, self.gen = [], 0.0, 0
        self.log_lines, self.log_view, self._thumb_key = [], None, None
        self.pub_state = ("Ready", MUTE, TILE)
        self.watching = False
        self.tray = None
        if pystray:
            try:
                self.tray = Tray(self)
                self._icon = ImageTk.PhotoImage(tray_image(32))
                r.iconphoto(True, self._icon)  # same cube on the window and taskbar
            except Exception as e:
                self.tray = None
                r.after(600, self.log, f"Tray icon unavailable ({e}). Running as a normal window.")
        self.build_panel()
        r.protocol("WM_DELETE_WINDOW", self.close)
        r.bind("<Escape>", lambda e: self.close())
        if self.tray:
            r.overrideredirect(True)  # a borderless panel by the tray, like Bambu Bar
        dark_titlebar(r)
        round_corners(r)
        if not pystray:
            # name the exact Python running this app: packages installed into a different
            # Python on the same computer (a common mix-up) aren't visible to it
            import sys
            exe = Path(sys.executable)
            if exe.name.lower() == "pythonw.exe":
                exe = exe.with_name("python.exe")  # same Python, but shows pip's progress
            r.after(600, self.log, "Tip: to keep the updater in the system tray, run this in PowerShell, "
                                   f"then restart the updater:\n& \"{exe}\" -m pip install pystray pillow")
        listen_for_second_launch(self)
        r.after(500, self.autostart)

    def autostart(self):
        """Begin watching on launch if it was on last time (on by default)."""
        if not self.cfg.get("auto", True):
            return
        if not (Path(self.v_root.get()) / "index.html").exists() or not self.v_projects.get():
            return self.log("Set the portfolio folder and Projects folder, then tick 'Watch Website Files folders'.")
        if not self.projects_ok():
            # At sign-in the network drive often connects a little after the app starts,
            # so keep trying every 30 seconds instead of giving up.
            if not getattr(self, "waiting", False):
                self.log("Projects folder not available yet (is the network drive connected?). "
                         "Retrying every 30 seconds.")
                self.waiting = True
            if not self.v_on.get():
                self.root.after(30000, self.autostart)
            return
        if self.v_on.get():
            return  # already turned on by hand
        self.waiting = False
        self.v_on.set(True)
        self.toggle()

    def run(self):
        self.root.mainloop()

    # ---------- the panel ----------
    def build_panel(self):
        r, fam = self.root, icon_family(self.root)
        r.configure(bg=BG)
        outer = plain(tk.Frame(r, bg=BG))
        outer.pack(fill="both", expand=True, padx=14, pady=(12, 12))
        L = lambda parent, **kw: plain(tk.Label(parent, bg=parent.cget("bg"), **{"anchor": "w", **kw}))

        # header: logo, name, watching status, and three small icon buttons
        hdr = plain(tk.Frame(outer, bg=BG))
        hdr.pack(fill="x", pady=(0, 10))
        if Image is not None:
            self._logo = ImageTk.PhotoImage(tray_image(30))
            L(hdr, image=self._logo).pack(side="left", padx=(0, 10))
        names = plain(tk.Frame(hdr, bg=BG))
        names.pack(side="left")
        L(names, text="Portfolio Updater", fg=INK, font=("Segoe UI Semibold", 12)).pack(anchor="w")
        st = plain(tk.Frame(names, bg=BG))
        st.pack(anchor="w")
        self.status_dot = L(st, text="●", fg=MUTE, font=("Segoe UI", 7))
        self.status_dot.pack(side="left", padx=(0, 5))
        self.status_text = L(st, text="Starting...", fg=MUTE, font=("Segoe UI", 9))
        self.status_text.pack(side="left")
        for icon, cmd in (("hide", self.close), ("settings", self.open_settings), ("refresh", self.refresh)):
            RoundButton(hdr, "", cmd, icon=icon, ifam=fam, color=BG, hover=BTN, fg=MUTE,
                        radius=9, padx=8, pady=6, size=10).pack(side="right", padx=(4, 0))

        # latest project: picture, publish status, title, newest version, date
        card = Card(outer, radius=16, padx=12, pady=12)
        card.pack(fill="x", pady=(0, 10))
        b = card.body
        self.thumb = L(b)
        self.thumb.grid(row=0, column=0, rowspan=4, sticky="nw", padx=(0, 12))
        self.pill = Pill(b)
        self.pill.grid(row=0, column=1, sticky="w")
        self.latest_title = L(b, fg=INK, font=("Segoe UI Semibold", 13))
        self.latest_title.grid(row=1, column=1, sticky="w", pady=(6, 0))
        self.latest_sub = L(b, fg=MUTE, font=("Segoe UI", 9))
        self.latest_sub.grid(row=2, column=1, sticky="w")
        self.latest_when = L(b, fg=MUTE, font=("Segoe UI", 9))
        self.latest_when.grid(row=3, column=1, sticky="sw", pady=(6, 0))
        b.columnconfigure(1, weight=1)

        # three tiles, like Nozzle / Bed / Chamber
        tiles = plain(tk.Frame(outer, bg=BG))
        tiles.pack(fill="x", pady=(0, 10))
        self.tile_nums = {}
        for i, (key, icon, label) in enumerate((("shown", "globe", "On website"), ("hidden", "hidden", "Hidden"),
                                                ("versions", "history", "Versions"))):
            t = Card(tiles, radius=14, padx=12, pady=10)
            t.grid(row=0, column=i, sticky="ew", padx=(0 if i == 0 else 8, 0))
            tiles.columnconfigure(i, weight=1, uniform="tile")
            top = plain(tk.Frame(t.body, bg=CARD))
            top.pack(anchor="w")
            glyph, font = (ICONS[icon], (fam, 9)) if fam else (PLAIN[icon], ("Segoe UI Symbol", 9))
            L(top, text=glyph, fg=ACCENT, font=font).pack(side="left", padx=(0, 6))
            L(top, text=label, fg=MUTE, font=("Segoe UI", 9)).pack(side="left")
            self.tile_nums[key] = L(t.body, text="0", fg=INK, font=("Segoe UI Semibold", 20))
            self.tile_nums[key].pack(anchor="w", pady=(2, 0))

        # recent activity, like the AMS section: one colored dot per event
        act = Card(outer, radius=16, padx=14, pady=10)
        act.pack(fill="x", pady=(0, 10))
        top = plain(tk.Frame(act.body, bg=CARD))
        top.pack(fill="x", pady=(0, 4))
        L(top, text="Recent activity", fg=MUTE, font=("Segoe UI Semibold", 9)).pack(side="left")
        Link(top, "Full log", self.show_log, font=("Segoe UI", 9)).pack(side="right")
        self.act_rows = []
        rows = plain(tk.Frame(act.body, bg=CARD))
        rows.pack(fill="x")
        rows.columnconfigure(1, weight=1)
        for i in range(5):  # always five lines, so the panel never changes height
            dot = L(rows, text="●", fg=CARD, font=("Segoe UI", 7))
            dot.grid(row=i, column=0, sticky="w", padx=(0, 8), pady=2)
            txt = L(rows, text=" ", fg=INK, font=("Segoe UI", 9))
            txt.grid(row=i, column=1, sticky="w")
            when = L(rows, text="", fg=MUTE, font=("Segoe UI", 8), anchor="e")
            when.grid(row=i, column=2, sticky="e", padx=(8, 0))
            self.act_rows.append((dot, txt, when))

        # big buttons, like Pause / Stop
        acts = plain(tk.Frame(outer, bg=BG))
        acts.pack(fill="x", pady=(0, 10))
        for i, (text, icon, cmd, color, hover, fg) in enumerate((
                ("Add version", "add", self.manual, RED_TINT, RED_TINT_HI, ACCENT),
                ("Projects", "list", self.manage, BTN, BTN_HI, INK),
                ("Scan", "search", self.scan, BTN, BTN_HI, INK))):
            RoundButton(acts, text, cmd, icon=icon, ifam=fam, color=color, hover=hover, fg=fg).grid(
                row=0, column=i, sticky="ew", padx=(0 if i == 0 else 8, 0))
            acts.columnconfigure(i, weight=1, uniform="act")

        # footer links, like "Open Bambu Bar" / "Quit"
        foot = plain(tk.Frame(outer, bg=BG))
        foot.pack(fill="x")
        if fam:
            L(foot, text=ICONS["open"], fg=MUTE, font=(fam, 9)).pack(side="left", padx=(2, 6))
        Link(foot, "Open website", self.open_site).pack(side="left")
        Link(foot, "Quit", self.quit, hover=ACCENT).pack(side="right")

        r.update_idletasks()
        r.geometry(f"{PANEL_W}x{r.winfo_reqheight()}")
        r.resizable(False, False)
        self.refresh_panel()

    def refresh_panel(self):
        """Bring the panel up to date: status, latest project, counts and activity."""
        if not hasattr(self, "pill"):
            return
        cut = lambda s, n: s if len(s) <= n else s[:n - 1].rstrip() + "…"
        # watching status
        if self.watching:
            n = len(self.web_dirs)
            self.status_dot.config(fg=GREEN)
            self.status_text.config(text=f"Watching {n} Website Files folder{'' if n == 1 else 's'}")
        elif self.v_on.get():
            self.status_dot.config(fg=AMBER)
            self.status_text.config(text="Starting to watch...")
        else:
            self.status_dot.config(fg=MUTE)
            self.status_text.config(text="Not watching for new exports")
        self.pill.set(*self.pub_state)
        # the project list
        try:
            projects = self.data()["projects"]
        except Exception:
            projects = []
        shown = [p for p in projects if not p.get("hidden")]
        self.tile_nums["shown"].config(text=str(len(shown)))
        self.tile_nums["hidden"].config(text=str(len(projects) - len(shown)))
        self.tile_nums["versions"].config(text=str(sum(len(p.get("versions", [])) for p in projects)))
        when = lambda p: max([p.get("modified") or ""] + [v.get("date") or "" for v in p.get("versions", [])])
        latest = max(shown, key=when, default=None)
        if latest is None:
            self.latest_title.config(text="No projects on the website yet")
            self.latest_sub.config(text="Scan your Projects folder to add some")
            self.latest_when.config(text="")
        else:
            vs = latest.get("versions", [])
            self.latest_title.config(text=cut(latest["title"], 22))
            self.latest_sub.config(text=cut(f"{vs[-1].get('id', '')}  {vs[-1].get('source', '')}".strip(), 32)
                                   if vs else "No 3D models yet")
            self.latest_when.config(text=f"Updated {self.nice_date(when(latest))}    "
                                         f"{len(vs)} version{'' if len(vs) == 1 else 's'}")
        self.set_thumb(latest)
        # recent activity (newest first)
        events = [e for e in reversed(self.log_lines) if not e[1].startswith(("Publishing", "Looking for"))][:5]
        for i, (dot, txt, tm) in enumerate(self.act_rows):
            if i < len(events):
                stamp, line = events[i]
                dot.config(fg=self.event_color(line))
                txt.config(text=cut(line.replace("\n", " "), 40))
                tm.config(text=f"{stamp:%I:%M %p}".lstrip("0"))
            else:
                dot.config(fg=CARD)
                txt.config(text=" " if i else "Nothing yet")
                tm.config(text="")

    @staticmethod
    def event_color(line):
        low = line.lower()
        if line.startswith("Published"):
            return GREEN
        if "failed" in low or "could not" in low or "error" in low or "not available" in low:
            return ACCENT
        if any(w in low for w in ("saved", "renamed", "deleted", "added", "updated", "showing", "hiding",
                                  "removed", "is now", "new card")):
            return "#ff938b"
        return MUTE

    @staticmethod
    def nice_date(stamp):
        if not stamp:
            return ""
        try:
            d = datetime.fromisoformat(stamp)
        except ValueError:
            return stamp
        today = date.today()
        if d.date() == today:
            return "today" + (f" {d:%I:%M %p}".replace(" 0", " ") if "T" in stamp else "")
        if (today - d.date()).days == 1:
            return "yesterday"
        return f"{d:%b} {d.day}" + ("" if d.year == today.year else f", {d.year}")

    def set_thumb(self, proj):
        """The latest project's card picture (or a dim cube), cached until the picture changes."""
        if Image is None:
            return self.thumb.config(text="")
        path = Path(self.v_root.get()) / proj["image"] if proj and proj.get("image") else None
        try:
            key = (str(path), path.stat().st_mtime) if path and path.is_file() else None
        except OSError:
            key = None
        if key == self._thumb_key and getattr(self, "_thumb", None):
            return
        self._thumb_key = key
        try:
            self._thumb = thumb_photo(path) if key else placeholder_photo()
        except Exception:
            self._thumb = placeholder_photo()
        self.thumb.config(image=self._thumb)

    def open_settings(self):
        SettingsDialog(self)

    def show_log(self):
        if self.log_view and self.log_view.winfo_exists():
            return self.log_view.lift()
        self.log_view = LogDialog(self)

    def toggle_panel(self):
        """Left-click on the tray icon: open the panel, or tuck it away if it's open."""
        if self.root.state() == "normal":
            self.close()
        else:
            self.show()

    def log(self, text):
        stamp = datetime.now()
        self.log_lines.append((stamp, text))
        del self.log_lines[:-500]
        if self.log_view and self.log_view.winfo_exists():
            self.log_view.add(stamp, text)
        self.refresh_panel()

    def tlog(self, text):
        """log() that is safe to call from a background thread."""
        self.root.after(0, self.log, text)

    def job(self, work, done):
        """Run work() in the background (scans of network folders can be slow) and pass
        its result to done() back on the window's thread."""
        if self.busy:
            return self.log("Still working on the last task. Try again when it finishes.")
        self.busy = True
        def run():
            try:
                res = (True, work())
            except Exception as e:
                res = (False, e)
            self.root.after(0, self._finish, res, done)
        threading.Thread(target=run, daemon=True).start()

    def _finish(self, res, done):
        self.busy = False
        ok, val = res
        if not ok:
            self.log(f"Failed: {val}")
            return messagebox.showerror("Something went wrong", str(val))
        done(val)

    def browse(self, var):
        d = filedialog.askdirectory(initialdir=var.get() or None)
        if d:
            var.set(d)

    def save_cfg(self):
        self.cfg.pop("watch", None)  # replaced by the Projects folder
        self.cfg.update(root=self.v_root.get(), publish=self.v_pub.get(),
                        auto=self.v_on.get(), projects=self.v_projects.get())
        try:
            CONFIG.parent.mkdir(parents=True, exist_ok=True)
            CONFIG.write_text(json.dumps(self.cfg), "utf-8")
        except OSError:
            pass

    def close(self):
        """The window's X: with the tray icon, hide to the tray and keep watching."""
        self.save_cfg()
        if not self.tray:
            return self.quit()
        self.root.withdraw()
        if not self.cfg.get("tray_told"):
            self.tray.note("Still running in the tray. Right-click the icon to quit.")
            self.cfg["tray_told"] = True

    def quit(self):
        self.save_cfg()
        if self.tray:
            self.tray.stop()
        self.root.destroy()

    def show(self):
        """Bring the window up in the bottom-right corner, just above the taskbar and next to
        the tray icon, like a tray app's panel. This happens every time, whether the window was
        hidden, minimized or already open somewhere else on the screen."""
        r = self.root
        if self.tray:
            self.place_bottom_right()  # before it appears, so it shows up in the corner...
        r.deiconify()
        if self.tray:
            r.after(30, self.place_bottom_right)  # ...and again once it's on screen, which Windows needs
        r.lift()
        r.attributes("-topmost", True)  # come in front of other windows...
        r.after(200, lambda: r.attributes("-topmost", False))  # ...without staying pinned on top
        r.focus_force()

    def place_bottom_right(self, gap=12):
        r = self.root
        r.update_idletasks()
        left, top, right, bottom = work_area(r)
        m = re.match(r"(\d+)x(\d+)", r.geometry())  # the window's inside size
        w, h = (int(m.group(1)), int(m.group(2))) if m else (r.winfo_width(), r.winfo_height())
        # the title bar and borders sit outside that size; measure them once the window is up
        border = r.winfo_rootx() - r.winfo_x()
        title = r.winfo_rooty() - r.winfo_y()
        if r.overrideredirect():
            border = title = 0  # the borderless panel has no title bar or frame
        elif not (0 <= border <= 20 and 10 <= title <= 80):
            border, title = 8, 31  # typical Windows values, used before the window has been shown
        if r.winfo_ismapped():  # once it's on screen, trust its real size
            w, h = max(w, r.winfo_width()), max(h, r.winfo_height())
        x = max(left, right - w - 2 * border - gap)
        y = max(top, bottom - h - title - border - gap)
        r.geometry(f"+{x}+{y}")
        # Windows sometimes ignores Tk's move for a borderless window, so also move it directly
        if os.name == "nt" and r.winfo_ismapped():
            try:
                import ctypes
                hwnd = ctypes.windll.user32.GetParent(r.winfo_id())
                ctypes.windll.user32.SetWindowPos(hwnd, 0, x, y, 0, 0, 0x0001 | 0x0004 | 0x0010)  # keep size, order, focus
            except Exception:
                pass

    def toggle_from_tray(self):
        self.v_on.set(not self.v_on.get())
        self.toggle()

    def open_site(self):
        import webbrowser
        url = site_url(self.v_root.get())
        if url:
            webbrowser.open(url)
        else:
            self.show()
            self.log("Couldn't work out the website's address (is the portfolio folder a git repository?).")

    def ask(self, path, project=None, folder=None):
        """Open "What did you change?"; if the window was hidden in the tray, hide it again after."""
        hidden = self.root.state() == "withdrawn"
        if hidden:
            self.show()  # pops up in the bottom-right corner, like the tray panel
        else:
            self.root.lift()
        Prompt(self, path, project, folder)  # waits until it's answered or skipped
        if hidden and self.tray:
            self.root.withdraw()

    def data(self):
        return load_data(self.v_root.get())

    def ready(self):
        if not (Path(self.v_root.get()) / "index.html").exists():
            messagebox.showwarning("Portfolio folder", "Choose the portfolio folder (the one containing "
                                   "index.html) in Settings: the gear icon at the top of the panel.")
            return False
        return True

    def projects_ok(self):
        try:
            return bool(self.v_projects.get()) and Path(self.v_projects.get()).is_dir()
        except OSError:
            return False

    def toggle(self):
        self.gen += 1  # stops any earlier watch loop
        self.watching = False
        if not self.v_on.get():
            if self.tray:
                self.tray.tip("not watching")
            return self.log("Stopped watching.")
        if not self.ready() or not self.projects_ok():
            self.v_on.set(False)
            return messagebox.showwarning("Projects folder", "Choose your Projects folder first (for example //TOAST-ARCHIVES/Projects).")
        # Only exports made from now on open the prompt; files already there are left alone.
        self.watch_from = time.time() - 5
        self.seen, self.pending, self.baselined = {}, {}, True
        self.log("Looking for Website Files folders in " + self.v_projects.get() + " ...")
        self.find_dirs(self.gen, start=True)

    def find_dirs(self, gen, start=False):
        """Find the Website Files folders in the background, then (re)start polling.
        Repeats every 10 minutes so newly created Website Files folders are picked up."""
        if gen != self.gen:
            return
        pdir = self.v_projects.get()
        def run():
            try:
                dirs, err = find_web_dirs(pdir), None
            except OSError as e:
                dirs, err = None, e
            self.root.after(0, found, dirs, err)
        def found(dirs, err):
            if gen != self.gen:
                return
            if dirs is None:
                self.log(f"Could not read the Projects folder: {err}")
            else:
                if start or len(dirs) != len(self.web_dirs):
                    self.log(f"Watching {len(dirs)} Website Files folder(s).")
                self.web_dirs = dirs
                self.watching = True
                if self.tray:
                    self.tray.tip(f"watching {len(dirs)} Website Files folder(s)")
                self.refresh_panel()  # the header's green "Watching" dot
            if start:
                self.poll(gen)
            self.root.after(600000, self.find_dirs, gen)
        threading.Thread(target=run, daemon=True).start()

    @staticmethod
    def sig(p):
        s = p.stat()
        return (s.st_mtime, s.st_size)

    def steps(self):
        """(file, its project folder) for every STEP file and picture in the Website Files folders."""
        out = []
        for proj_dir, web in self.web_dirs:
            try:
                out += [(p, proj_dir) for p in web_files(web, STEP_EXT + IMAGE_EXT)]
            except OSError:
                pass  # a folder that was moved or deleted
        return out

    def update_covers(self, folders):
        """Set each project's card picture from the newest picture in its Website Files.
        folders: {project folder: [(modified time, picture path)]}."""
        if self.busy:  # another task is running: try again shortly instead of dropping it
            return self.root.after(5000, self.update_covers, folders)
        root = Path(self.v_root.get())
        imported = self.cfg.setdefault("imported", {})
        @locked
        def work():
            data, links, changed = load_data(root), self.cfg.get("links", {}), []
            for proj_dir, images in folders.items():
                p = match_project(data, links, proj_dir)
                if p is None:
                    self.tlog(f"Picture in {Path(proj_dir).name} kept for later: that folder has no card yet.")
                elif not p.get("hidden") and import_cover(root, p, sorted(images), imported):
                    p.setdefault("folder", str(proj_dir))
                    changed.append(p["title"])
            if changed:
                save_data(root, data)
            return changed
        def done(changed):
            for t in changed:
                self.log(f"{t}: card picture updated.")
            if changed:
                self.push(root, "Update card picture: " + ", ".join(changed))
        self.job(work, done)

    def target(self, proj_dir):
        """The project title for a project folder, or None if it has no card yet."""
        p = match_project(self.data(), self.cfg.get("links", {}), proj_dir)
        return p["title"] if p else None

    def poll(self, gen):
        if gen != self.gen or not self.v_on.get():
            return
        try:
            for p, proj_dir in self.steps():
                s = self.sig(p)
                if self.seen.get(p) == s:
                    continue
                if p not in self.seen and p not in self.pending and s[0] < self.watch_from:
                    self.seen[p] = s  # was already there before watching started
                    continue
                if self.pending.get(p) == s:  # unchanged for one poll: the file has finished copying
                    self.seen[p] = s
                    self.pending.pop(p)
                    if p.suffix.lower() in IMAGE_EXT:  # a picture: becomes the card picture, no questions
                        self.update_covers({proj_dir: [(s[0], p)]})
                        continue
                    self.ask(p, self.target(proj_dir), proj_dir)
                else:
                    self.pending[p] = s
        except OSError as e:
            self.log(f"Watch error: {e}")
        self.root.after(3000, self.poll, gen)

    def refresh(self):
        """Recheck every Website Files folder now. Files that are already on a project
        are compared part by part with that window's latest version, so changes made
        while the app was closed are caught too."""
        if not self.ready():
            return
        if not self.projects_ok():
            return self.log("Refresh failed: the Projects folder is not available (is the network drive connected?).")
        self.log("Refresh: looking through the Website Files folders...")
        pdir = self.v_projects.get()
        def work():
            dirs = find_web_dirs(pdir)
            files, pics = [], {}
            for proj_dir, web in dirs:
                files += [(p, proj_dir) for p in web_steps(web)]
                for img in web_files(web, IMAGE_EXT):
                    try:
                        pics.setdefault(proj_dir, []).append((img.stat().st_mtime, img))
                    except OSError:
                        pass
            return dirs, files, pics
        def done(res):
            self.web_dirs, files, pics = res
            self.refresh_files(files)
            if pics:
                self.update_covers(pics)
        self.job(work, done)

    def refresh_files(self, files):
        root = Path(self.v_root.get())
        self.log(f"Checking {len(files)} STEP file(s)...")
        self.root.update_idletasks()
        data, links, found, skipped = self.data(), self.cfg.get("links", {}), 0, 0
        for p, proj_dir in files:
            s = self.sig(p)
            proj = (next((x for x in data["projects"] if x["title"] == links.get(str(p))), None)
                    or match_project(data, links, proj_dir))
            if proj and proj.get("hidden"):
                continue  # hidden projects are left alone
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
            self.ask(p, proj["title"] if proj else None, proj_dir)
        if skipped:
            self.log(f"{skipped} other STEP file(s) skipped. Use 'Scan projects folder' to add files that aren't on the site yet.")
        if not found:
            self.log("Refresh: no part changes found.")

    def folder_for(self, path):
        """The project folder (a direct subfolder of the Projects folder) a file sits in,
        or None if the file is somewhere else."""
        pd = str(self.v_projects.get()).replace("\\", "/").rstrip("/")
        s = str(path).replace("\\", "/")
        if not pd or not s.lower().startswith(pd.lower() + "/"):
            return None
        first = s[len(pd) + 1:].split("/")[0]
        return Path(pd) / first if first and "/" in s[len(pd) + 1:] else None

    def manual(self):
        if not self.ready():
            return
        f = filedialog.askopenfilename(filetypes=[("STEP files", "*.step *.stp")],
                                       initialdir=self.cfg.get("last_dir") or self.v_projects.get() or None)
        if not f:
            return
        p = Path(f)
        self.cfg["last_dir"] = str(p.parent)  # the next "Add a version manually" starts here
        folder = self.folder_for(p)
        # pick the project the same way the watcher does: by the project folder the file is in,
        # or, for a file outside the Projects folder, by the project it was saved to before
        project = self.target(folder) if folder else self.cfg.get("links", {}).get(str(p))
        if folder:
            self.log(f"{p.name} is in the {folder.name} folder"
                     + (f", so it goes to {project}." if project else ", which has no card yet: starting a new project."))
        Prompt(self, p, project, folder)

    @exclusive
    def save(self, path, choice, note, new, picture="", adopt=False, folder=None):
        root = Path(self.v_root.get())
        try:
            data = load_data(root)
            if new:
                proj = {"id": unique_id(data, new["Title"]), "title": new["Title"], "summary": new["Summary"],
                        "tags": [t.strip() for t in new["Tags"].split(",") if t.strip()], "versions": []}
                data["projects"].append(proj)
            else:
                proj = next(p for p in data["projects"] if p["title"] == choice)
            if folder:
                proj.setdefault("folder", str(folder))  # ties the card to its project folder
            try:  # so a later scan doesn't import this same file a second time
                self.cfg.setdefault("imported", {})[npath(path)] = Path(path).stat().st_mtime
            except OSError:
                pass
            msg = add_version(root, proj, note, path, adopt)
            proj["modified"] = datetime.now().isoformat(timespec="seconds")  # moves its card to the front
            if proj.get("hidden"):
                msg += " (This project is hidden from the website. Show it in Manage projects.)"
            if picture:
                size = set_cover(root, proj, picture)
                proj.pop("image_from_folder", None)  # chosen by hand: scans won't replace it
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
            self.pub_state = ("Saved, not published", MUTE, TILE)
            return self.refresh_panel()
        if (Path(root) / ".git").exists():
            self.pub_state = ("Publishing", AMBER, AMBER_TINT)
            self.log("Publishing...")
            publish(root, message, lambda t: self.root.after(0, self.published, message, t))
        else:
            self.pub_state = ("Not published", ACCENT, RED_TINT)
            self.log("Not published: the portfolio folder is not a git repository.")

    def published(self, message, result):
        ok = not result.startswith("Publish failed")
        self.pub_state = ("Published", GREEN, GREEN_TINT) if ok else ("Publish failed", ACCENT, RED_TINT)
        self.log(result)
        # tucked away in the tray? say so with a Windows notification instead
        if self.tray and self.root.state() == "withdrawn":
            self.tray.note(f"{message}\n{result}" if result.startswith("Publish failed") else f"Published: {message}")

    @exclusive
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

    @exclusive
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

    # ---- projects folder scan ----
    def scan(self):
        if not self.ready():
            return
        pdir = self.v_projects.get()
        try:
            ok = bool(pdir) and Path(pdir).is_dir()
        except OSError:
            ok = False
        if not ok:
            return messagebox.showwarning("Projects folder", "Choose your Projects folder first (for example //TOAST-ARCHIVES/Projects).")
        root = Path(self.v_root.get())
        self.log(f"Scanning {pdir} ... (network folders can take a minute)")
        ignored = set(self.cfg.get("ignored_folders", []))
        def work():
            data, links, found = load_data(root), self.cfg.get("links", {}), []
            for d in project_folders(pdir):
                if npath(d) in ignored:  # cards you deleted stay deleted
                    continue
                newest, steps, images = scan_folder(d)
                found.append((d, newest, steps, images, match_project(data, links, d)))
            return found
        self.job(work, self.scan_found)

    def scan_found(self, found):
        new = [d for d, _, _, _, p in found if p is None]
        self.log(f"Found {len(found)} project folder(s): {len(new)} new, {len(found) - len(new)} already have a card.")
        show = False
        if new:
            names = ", ".join(d.name for d in new[:15]) + (f", and {len(new) - 15} more" if len(new) > 15 else "")
            ans = messagebox.askyesnocancel(
                "New project folders",
                f"Found {len(new)} folder(s) without a card: {names}.\n\n"
                "Show their cards on the website right away?\n\n"
                "Yes: the cards go live now, with the STEP files and pictures from each "
                "folder's 'Website Files' folder.\n"
                "No: the cards are created hidden. In Manage projects, tick 'Show this project on "
                "the website' for the ones you want; their Website Files are added then.\n"
                "Cancel: add nothing.")
            if ans is None:
                return self.log("Scan cancelled. Nothing was added.")
            show = ans
        root, links = Path(self.v_root.get()), self.cfg.get("links", {})
        imported = self.cfg.setdefault("imported", {})
        @locked
        def work():
            data = load_data(root)
            made = models = dated = 0
            for d, newest, steps, images, _ in found:
                p = match_project(data, links, d)
                if p is None:
                    p = {"id": unique_id(data, d.name), "title": d.name, "summary": "", "tags": [],
                         "versions": [], "folder": str(d), "imported": True}
                    if not show:
                        p["hidden"] = True
                    data["projects"].append(p)
                    made += 1
                p.setdefault("folder", str(d))
                m = iso(newest)
                if m > (p.get("modified") or ""):
                    p["modified"] = m
                    dated += 1
                if p.get("hidden"):
                    continue  # hidden cards get their files when they're shown
                # every shown card takes its picture from Website Files
                if import_cover(root, p, images, imported):
                    self.tlog(f"{p['title']}: card picture updated.")
                    dated += 1
                # STEP files are copied in only for folders the scan created; other
                # projects get theirs through the "What did you change?" window
                if p.get("imported"):
                    n = import_models(root, p, steps, imported, self.tlog)
                    if n:
                        self.tlog(f"{p['title']}: added {n} model version(s).")
                    models += n
            if made or models or dated:
                save_data(root, data)
            return made, models, dated
        self.log("Updating cards" + (" and copying STEP files..." if show else "..."))
        def done(res):
            made, models, dated = res
            if not (made or models or dated):
                return self.log("Scan finished: everything was already up to date.")
            self.log(f"Scan finished: {made} new card(s)" + ("" if show or not made else " (hidden)")
                     + f", {models} model version(s) added, {dated} card date or picture update(s).")
            self.push(root, f"Scan projects folder: {made} new, {models} models")
        self.job(work, done)

    def set_visible(self, titles, show, after=None):
        """Show or hide one or more projects' cards, published as one update. Showing a
        project also brings in its Website Files picture (and, for scanned projects, its
        STEP files)."""
        if isinstance(titles, str):
            titles = [titles]
        root = Path(self.v_root.get())
        imported = self.cfg.setdefault("imported", {})
        @locked
        def work():
            data, added = load_data(root), {}
            for title in titles:
                p = next(x for x in data["projects"] if x["title"] == title)
                if show:
                    p.pop("hidden", None)
                else:
                    p["hidden"] = True
                if show and p.get("folder") and Path(p["folder"]).is_dir():
                    _, steps, images = scan_folder(Path(p["folder"]))
                    if p.get("imported"):
                        added[title] = import_models(root, p, steps, imported, self.tlog)
                    import_cover(root, p, images, imported)
            save_data(root, data)
            return added
        names = ", ".join(titles)
        self.log(f"Showing {names} on the website..." if show else f"Hiding {names} from the website...")
        def done(added):
            for title in titles:
                n = added.get(title, 0)
                self.log((f"{title} is now on the website" + (f", with {n} model version(s) added." if n else "."))
                         if show else f"{title} is now hidden from the website.")
            self.push(root, f"{'Show' if show else 'Hide'} {names}"[:120])
            if after:
                after()
        self.job(work, done)

    @exclusive
    def edit_tag(self, old, new):
        """Rename a tag on every project (merging it if the new name already exists),
        or remove it everywhere when new is None."""
        root = Path(self.v_root.get())
        try:
            data, changed = load_data(root), []
            for p in data["projects"]:
                tags = p.get("tags") or []
                if old.lower() not in {t.lower() for t in tags}:
                    continue
                out, seen = [], set()
                for t in tags:
                    if t.lower() == old.lower():
                        if new is None:
                            continue
                        t = new
                    if t.lower() not in seen:  # merging can create a duplicate: keep one
                        seen.add(t.lower())
                        out.append(t)
                p["tags"] = out
                changed.append(p["title"])
            if changed:
                save_data(root, data)
        except Exception as e:
            messagebox.showerror("Could not change tag", str(e))
            return False
        if not changed:
            return True
        what = f'Removed the tag "{old}"' if new is None else f'Renamed the tag "{old}" to "{new}"'
        self.log(f"{what} on {len(changed)} project(s): {', '.join(changed)}.")
        self.push(root, (f'Remove tag "{old}"' if new is None else f'Rename tag "{old}" to "{new}"'))
        return True

    @exclusive
    def set_details(self, title, text, tags, new_title=None):
        """Save a project's description and tags, and optionally a new title. The title is
        only what the website shows: the project folder and the page address stay the same."""
        root = Path(self.v_root.get())
        new_title = new_title or title
        try:
            data = load_data(root)
            proj = next(p for p in data["projects"] if p["title"] == title)
            old_id = proj["id"]
            change_id(root, data, proj, new_title)  # the page address follows the title
            proj["title"] = new_title  # the project's folder on TOAST-ARCHIVES keeps its name
            proj["summary"] = text
            proj["tags"] = tags
            save_data(root, data)
        except Exception as e:
            messagebox.showerror("Could not save", str(e))
            return False
        if proj["id"] != old_id and new_title == title:
            self.log(f"{title}: its page is now #/{proj['id']} (the old #/{old_id} link still works).")
        if new_title != title:
            # the updater keeps track of projects by title, so follow the rename
            links = self.cfg.get("links", {})
            for k in [k for k, t in links.items() if t == title]:
                links[k] = new_title
            if self.cfg.get("last") == title:
                self.cfg["last"] = new_title
            self.log(f'Renamed "{title}" to "{new_title}" on the website.'
                     + (f" Its page is now #/{proj['id']} (the old #/{old_id} link still works)."
                        if proj["id"] != old_id else ""))
        self.log(f"Saved {new_title}" + (f" (tags: {', '.join(tags)})." if tags else " (no tags)."))
        self.push(root, f"{new_title}: update title, description and tags"
                  if new_title != title else f"{new_title}: update description and tags")
        return True

    @exclusive
    def remove_project(self, title):
        root = Path(self.v_root.get())
        try:
            data = load_data(root)
            proj = next(p for p in data["projects"] if p["title"] == title)
            data["projects"].remove(proj)
            d = root / "projects" / proj["id"]
            if proj.get("id") and d.is_dir() and inside(root, d):
                shutil.rmtree(d, ignore_errors=True)
            save_data(root, data)
        except Exception as e:
            messagebox.showerror("Could not delete", str(e))
            return False
        links = self.cfg.get("links", {})
        for path in [k for k, t in links.items() if t == title]:
            links.pop(path)
        if self.cfg.get("last") == title:
            self.cfg.pop("last")
        if proj.get("folder"):  # so the next scan doesn't bring the card back
            ignored = self.cfg.setdefault("ignored_folders", [])
            if npath(proj["folder"]) not in ignored:
                ignored.append(npath(proj["folder"]))
        self.log(f"Deleted the project {title}.")
        self.push(root, f"Delete project {title}")
        return True


if __name__ == "__main__":
    import sys
    if already_running():  # opened twice: bring the running one forward instead
        sys.exit(0)
    app = App()
    if "--minimized" in sys.argv:  # used by the Windows startup shortcut
        if app.tray:
            app.root.withdraw()  # start quietly in the tray
        else:
            app.root.iconify()
    elif app.tray:
        app.root.after(50, app.show)  # opened by hand: appear in the corner by the tray icon
    app.run()
