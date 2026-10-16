#!/usr/bin/env python3
"""file-namer: init -> scan -> plan -> (visual) -> apply. Renames and sorts files on macOS. Stdlib only.
Never deletes or overwrites: every change is `mv -n`, logged to rename-log.csv. See README.md."""
import argparse, csv, hashlib, json, os, re, shutil, subprocess, sys, time, unicodedata
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
PREFIXES = ("ss_", "vid_", "img_", "doc_", "aud_", "zip_", "app_", "misc_")
CONVENTION = re.compile(r"^(ss|vid|img|doc|aud|zip|app|misc)_(?=[a-z0-9-]*[a-z])[a-z0-9]+(-[a-z0-9]+)*(\.[A-Za-z0-9]+)?$")
SKIP_DIRS = {"Library", "System", "Applications", "node_modules", "__pycache__", ".Trash",
             "venv", ".venv", "site-packages", "Pods", "DerivedData"}
EXT = {
    "image": {"png", "jpg", "jpeg", "heic", "heif", "webp", "gif", "tif", "tiff", "bmp", "svg"},
    "video": {"mov", "mp4", "m4v", "avi", "mkv", "webm"},
    "audio": {"m4a", "mp3", "wav", "aac", "flac", "ogg", "aiff"},
    "doc": {"pdf", "doc", "docx", "rtf", "txt", "md", "pages", "key", "ppt", "pptx", "numbers",
            "xls", "xlsx", "csv", "tsv", "vtt", "srt", "html", "htm", "json", "epub"},
    "archive": {"zip", "rar", "7z", "tar", "gz", "tgz", "bz2", "xz"},
    "installer": {"dmg", "pkg", "mpkg", "iso", "exe", "msi"},
}
CATEGORY_DIR = {"screenshot": "screenshots", "screen_recording": "videos", "video": "videos",
                "image": "images", "audio": "audio", "doc": "docs", "archive": "archives",
                "installer": "installers", "app": "installers", "other": "other"}
PREFIX_FOR = {"screenshot": "ss_", "screen_recording": "vid_", "video": "vid_", "image": "img_",
              "audio": "aud_", "doc": "doc_", "archive": "zip_", "installer": "app_", "app": "app_",
              "other": "misc_"}
GENERIC_NAME = re.compile(
    r"^(screenshot|screen shot|screen recording|img[_-]?\d|image|photo|untitled|download|unnamed|file|"
    r"document|scan|pxl_|dsc|dji|gmt\d|capture|clip|video|recording|new recording|[0-9a-f]{8}-|[0-9a-f-]{12,}|\d+$|\d{4}-\d{2}-\d{2})",
    re.I)
SENSITIVE_TEXT = re.compile(r"(password|passwd|api[_ -]?key|secret[_ -]?key|private key|BEGIN [A-Z ]*KEY|"
                            r"sk_live_|sk-[A-Za-z0-9]{20}|AKIA[0-9A-Z]{16}|seed phrase|recovery phrase)", re.I)
SENSITIVE_NAME = re.compile(r"(\bkey\b|credential|secret|\.pem$|\.p12$|service[-_ ]account|password|token)", re.I)
CHROME_NOISE = re.compile(r"^(chrome|file|edit|view|history|bookmarks|profiles|tab|window|help|finish update|"
                          r"ask gemini|work|share|upgrade|convert to pdf|relaunch to update|all bookmarks|"
                          r"q|x|\W+)$", re.I)
STOP = set("the a an and or of to for in on at by with from is are be this that it as your you we our my me i "
           "just now today yesterday ago minutes minute hours hour am pm reply replies new show more less app "
           "x com www https http gmail".split())

# ---------------------------------------------------------------- privacy
USER_PRIVATE_FILE = os.path.expanduser("~/.config/file-namer/private-folders.txt")
USER_IGNORE_FILE = os.path.expanduser("~/.config/file-namer/ignore.txt")
DONOTREAD = ".donotread"
INIT_QUESTION = "Are there any folder names, file names, or keywords you want us to ignore?"

def read_ignore_list(path=USER_IGNORE_FILE):
    """ignore.txt: one entry per line or comma-separated; '#' starts a comment. Returns lowercased entries."""
    out = []
    if path and os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            line = line.split("#", 1)[0]
            out += [e.strip().lower() for e in line.split(",") if e.strip()]
    return list(dict.fromkeys(out))

class PrivacyRules:
    """Decides whether a path is private (user ignore list, .donotread marker, private-folders list, --private-dir,
    --private-re). Private files are never OCR'd/read, never sent to a visual look, never named from their content
    and never moved; plans and previews only report how many there are."""
    def __init__(self, root, private_re=None, private_dirs=None, user_file=USER_PRIVATE_FILE,
                 ignore_file=USER_IGNORE_FILE):
        self.root = os.path.abspath(root)
        self._dnr = {}
        # ignore list: entries < 3 chars must equal a whole path component (or its name without extension);
        # longer entries match case-insensitively inside any component at letter boundaries.
        self.ignore_exact, ign = set(), []
        for e in read_ignore_list(ignore_file):
            self.ignore_exact.add(e)
            if len(e) >= 3:
                ign.append(re.escape(e))
        self.ignore_re = re.compile(rf"(?<![a-z])(?:{'|'.join(ign)})(?![a-z])", re.I) if ign else None
        self.regex = re.compile(private_re, re.I) if private_re else None
        dirs = list(private_dirs or [])
        if user_file and os.path.exists(user_file):
            for line in open(user_file, encoding="utf-8"):
                line = line.strip()
                if line and not line.startswith("#"):
                    dirs.append(line)
        self.dirs = []
        for d in dirs:
            d = os.path.expanduser(d)
            self.dirs.append(os.path.abspath(d if os.path.isabs(d) else os.path.join(self.root, d)))

    def _donotread(self, d):
        """True if folder d or any ancestor contains a .donotread marker (cached)."""
        chain = []
        while True:
            if d in self._dnr:
                hit = self._dnr[d]; break
            chain.append(d)
            if os.path.exists(os.path.join(d, DONOTREAD)):
                hit = True; break
            parent = os.path.dirname(d)
            if parent == d:
                hit = False; break
            d = parent
        for c in chain:
            self._dnr[c] = hit
        return hit

    def check(self, path):
        """Return a reason string ('donotread', 'ignore-list', 'user-list', 'regex') or None."""
        path = os.path.abspath(path)
        if self._donotread(path if os.path.isdir(path) and not path.endswith(".app") else os.path.dirname(path)):
            return "donotread"
        if self.ignore_exact or self.ignore_re:
            for part in path.split(os.sep):
                if not part:
                    continue
                low = part.lower()
                if low in self.ignore_exact or low.rsplit(".", 1)[0] in self.ignore_exact or \
                        (self.ignore_re and self.ignore_re.search(part)):
                    return "ignore-list"
        for d in self.dirs:
            if path == d or path.startswith(d.rstrip(os.sep) + os.sep):
                return "user-list"
        rel = os.path.relpath(path, self.root)
        if self.regex and self.regex.search(rel):
            return "regex"
        return None

def rules_from_args(a, root):
    return PrivacyRules(root, getattr(a, "private_re", None), getattr(a, "private_dir", None))

# ---------------------------------------------------------------- helpers
def log(*a):
    print(*a, file=sys.stderr, flush=True)

def jl_append(path, obj):
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(obj, ensure_ascii=False) + "\n")

def jl_read(path):
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    pass

def slug(text, max_words=6):
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    words = [w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in STOP and len(w) > 1]
    out = []
    for w in words:
        if w not in out:
            out.append(w)
        if len(out) >= max_words:
            break
    return "-".join(out)

def classify(name, ext):
    low = name.lower()
    if ext == "app":
        return "app"
    if low.startswith(("screenshot", "screen shot", "cleanshot")) and ext in EXT["image"]:
        return "screenshot"
    if low.startswith("screen recording") and ext in EXT["video"]:
        return "screen_recording"
    for k, exts in EXT.items():
        if ext in exts:
            return k
    return "other"

def sha256(path, chunk=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            b = fh.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()

def walk(root, recursive, exclude):
    """Yield (path, is_app_bundle). Skips hidden, .git repos, system/app/library dirs, excluded dirs."""
    root = os.path.abspath(root)
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            entries = sorted(os.scandir(d), key=lambda e: e.name)
        except OSError as e:
            log("skip dir", d, e)
            continue
        names = {e.name for e in entries}
        if d != root and ".git" in names:
            continue  # inside a git repo: leave alone
        for e in entries:
            if e.name.startswith("."):
                continue
            if e.is_dir(follow_symlinks=False):
                if e.name.endswith(".app"):
                    yield e.path, True
                    continue
                if (e.name in SKIP_DIRS or e.name.endswith("_files") or e.path in exclude
                        or any(e.path.startswith(x + os.sep) for x in exclude)):
                    continue
                if recursive:
                    stack.append(e.path)
            elif e.is_file(follow_symlinks=False):
                yield e.path, False

# ---------------------------------------------------------------- scan
def cmd_scan(a):
    out = os.path.abspath(a.out)
    os.makedirs(out, exist_ok=True)
    inv_p, hash_p = os.path.join(out, "inventory.jsonl"), os.path.join(out, "hashes.jsonl")
    ocr_p, media_p = os.path.join(out, "ocr.jsonl"), os.path.join(out, "media.jsonl")
    exclude = {os.path.abspath(x) for x in (a.exclude or [])}
    root = os.path.abspath(a.root)
    rules = rules_from_args(a, root)
    helper = a.helper or os.path.join(HERE, "ocr_helper")
    with open(os.path.join(out, "scan-meta.json"), "w") as fh:
        json.dump({"root": root, "private_dirs": rules.dirs, "created": time.strftime("%Y-%m-%dT%H:%M:%S")}, fh, indent=1)

    # 1) inventory (streamed; resumable)
    done = {r["path"] for r in jl_read(inv_p)}
    n = 0
    for p, is_app in walk(root, a.recursive, exclude):
        if p in done:
            continue
        name = os.path.basename(p)
        if is_app:
            ext, size = "app", 0
            for dp, _, fs in os.walk(p):
                for f in fs:
                    try: size += os.lstat(os.path.join(dp, f)).st_size
                    except OSError: pass
        else:
            ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
            size = os.lstat(p).st_size
        st = os.lstat(p)
        jl_append(inv_p, {"path": p, "name": name, "rel": os.path.relpath(p, root), "ext": ext, "size": size,
                          "mtime": st.st_mtime, "btime": getattr(st, "st_birthtime", st.st_mtime), "kind": classify(name, ext), "app_bundle": is_app,
                          "depth": os.path.relpath(p, root).count(os.sep),
                          "private": rules.check(p)})
        n += 1
        if n % 1000 == 0:
            log(f"inventory {n}")
    inv = list(jl_read(inv_p))
    for r in inv:  # re-evaluate on resume (rules may have changed)
        r["private"] = rules.check(r["path"]) or r.get("private")
    log(f"inventory total {len(inv)}, private {sum(1 for r in inv if r.get('private'))}")

    # 2) exact dups: hash only files whose size collides
    if not a.no_hash:
        hashed = {r["path"] for r in jl_read(hash_p)}
        by_size = defaultdict(list)
        for r in inv:
            if not r["app_bundle"] and r["size"] > 0:
                by_size[r["size"]].append(r["path"])
        todo = [p for ps in by_size.values() if len(ps) > 1 for p in ps if p not in hashed]
        log(f"hashing {len(todo)} size-colliding files")
        for i, p in enumerate(todo, 1):
            try:
                jl_append(hash_p, {"path": p, "sha256": sha256(p)})
            except OSError as e:
                log("hash fail", p, e)
            if i % 200 == 0:
                log(f"hashed {i}/{len(todo)}")

    # 3) OCR images + PDF text via one streaming swift process
    if a.ocr:
        have = {r["path"] for r in jl_read(ocr_p)}
        targets = [r["path"] for r in inv if r["path"] not in have and r["size"] > 0
                   and not r.get("private") and (
            (r["kind"] in ("screenshot", "image") and r["ext"] != "svg" and
             (a.ocr_all or r["kind"] == "screenshot" or GENERIC_NAME.match(r["name"])))
            or r["ext"] == "pdf")]
        log(f"ocr/pdf targets {len(targets)}")
        if targets:
            proc = subprocess.Popen([helper], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1)
            for i, p in enumerate(targets, 1):
                proc.stdin.write(p + "\n"); proc.stdin.flush()
                line = proc.stdout.readline()
                if not line:
                    log("helper died at", p); break
                rec = json.loads(line)
                if "lines" in rec:  # keep it compact
                    rec["lines"] = [{k: (round(v, 3) if isinstance(v, float) else v) for k, v in l.items()}
                                    for l in rec["lines"][:200]]
                jl_append(ocr_p, rec)
                if i % 100 == 0:
                    log(f"ocr {i}/{len(targets)}")
            proc.stdin.close(); proc.wait()

    # 4) other docs: cheap text extraction (first ~2KB)
    if a.ocr:
        have = {r["path"] for r in jl_read(ocr_p)}
        for r in inv:
            if r["path"] in have or r["kind"] != "doc" or r["ext"] == "pdf" or r.get("private"):
                continue
            txt, ext = "", r["ext"]
            try:
                if ext in ("txt", "md", "csv", "tsv", "vtt", "srt", "json"):
                    with open(r["path"], encoding="utf-8", errors="ignore") as fh:
                        txt = fh.read(2000)
                elif ext in ("docx", "rtf", "html", "htm", "doc"):
                    txt = subprocess.run(["textutil", "-convert", "txt", "-stdout", r["path"]],
                                         capture_output=True, text=True, timeout=30).stdout[:2000]
                elif ext == "xlsx":
                    txt = subprocess.run(["unzip", "-p", r["path"], "xl/sharedStrings.xml"],
                                         capture_output=True, text=True, timeout=30).stdout[:6000]
                    txt = " | ".join(re.findall(r"<t[^>]*>([^<]+)</t>", txt)[:40])
            except Exception as e:
                r_err = str(e)
            title = ""
            try:
                md = subprocess.run(["mdls", "-raw", "-name", "kMDItemTitle", r["path"]],
                                    capture_output=True, text=True, timeout=10).stdout.strip()
                title = "" if md in ("(null)", "") else md
            except Exception:
                pass
            jl_append(ocr_p, {"path": r["path"], "mode": "text", "title": title, "text": txt})

    # 5) media: ffprobe (+ optional middle frame and whisper snippet)
    if a.media:
        have = {r["path"] for r in jl_read(media_p)}
        fdir = os.path.join(out, "frames"); os.makedirs(fdir, exist_ok=True)
        for r in inv:
            if r["kind"] not in ("video", "screen_recording", "audio") or r["path"] in have:
                continue
            rec = {"path": r["path"]}
            try:
                pr = json.loads(subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                     "format=duration:stream=codec_type,width,height", "-of", "json", r["path"]],
                     capture_output=True, text=True, timeout=60).stdout or "{}")
                rec["duration"] = float(pr.get("format", {}).get("duration", 0) or 0)
                v = [s for s in pr.get("streams", []) if s.get("codec_type") == "video"]
                if v: rec["res"] = f'{v[0].get("width")}x{v[0].get("height")}'
                rec["has_audio"] = any(s.get("codec_type") == "audio" for s in pr.get("streams", []))
            except Exception as e:
                rec["error"] = str(e)
            mid = rec.get("duration", 0) / 2
            key = hashlib.md5(r["path"].encode()).hexdigest()[:10]
            if r.get("private"):  # metadata only: no frame, no transcript
                jl_append(media_p, rec); continue
            if a.frames and rec.get("res"):
                fp = os.path.join(fdir, key + ".jpg")
                subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{mid:.2f}", "-i", r["path"], "-frames:v", "1",
                                "-vf", "scale=800:-2", "-q:v", "5", fp], timeout=120)
                if os.path.exists(fp):
                    rec["frame"] = fp
            if a.whisper_model and rec.get("has_audio") and rec.get("duration", 0) > 2:
                wav = os.path.join(fdir, key + ".wav")
                start = max(0, mid - 10)
                subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{start:.2f}", "-t", "20", "-i", r["path"],
                                "-ac", "1", "-ar", "16000", wav], timeout=120)
                if os.path.exists(wav):
                    w = subprocess.run(["whisper-cli", "-m", a.whisper_model, "-nt", "-np", "-f", wav],
                                       capture_output=True, text=True, timeout=300)
                    rec["transcript"] = " ".join(w.stdout.split())[:600]
                    os.remove(wav)
            jl_append(media_p, rec)
            log("media", r["name"], rec.get("res"), rec.get("duration"))
    log("scan done ->", out)

# ---------------------------------------------------------------- plan
def ocr_title(rec):
    """Best-effort title from OCR lines. Returns (slug, n_text_lines, strong).
    Prefers tall lines near the top with >=2 real words; ignores browser/menu chrome, URLs and numbers."""
    def words(t):
        return [w for w in re.findall(r"[A-Za-z][A-Za-z']+", t) if w.lower() not in STOP and len(w) > 2]
    lines = [l for l in rec.get("lines", []) if not CHROME_NOISE.match(l["t"].strip()) and "://" not in l["t"]
             and not re.search(r"\.(com|ai|io|net|org)\b", l["t"]) and len(words(l["t"])) >= 2]
    if not lines:
        return "", 0, False
    hs = sorted(l["h"] for l in lines)
    med = hs[len(hs) // 2] or 1e-6
    def score(l):
        return (l["h"] / med) * 2 - l["y"] * 1.5 + min(len(words(l["t"])), 5) * 0.3
    best = sorted(lines, key=score, reverse=True)
    t = slug(" ".join(words(best[0]["t"])), 6)
    if t.count("-") < 2 and len(best) > 1:
        t = slug(" ".join(words(best[0]["t"]) + words(best[1]["t"])), 5)
    strong = len(lines) >= 5 and t.count("-") >= 2 and best[0]["h"] >= med * 1.2
    return t, len(lines), strong

def clean_text(t):
    """Drop subtitle timestamps, cue numbers, WEBVTT headers and JSON punctuation before naming from text."""
    out = []
    for line in t.splitlines():
        line = line.strip()
        if not line or line.upper().startswith("WEBVTT") or "-->" in line or re.match(r"^[\d:.,\s-]+$", line):
            continue
        line = re.sub(r"^[^:]{1,40}:\s", "", line) if re.match(r"^[A-Z][\w .'-]{1,40}:\s", line) else line
        out.append(line)
        if len(out) >= 6:
            break
    return " ".join(out)

def tokens(rec):
    t = " ".join(l["t"] for l in rec.get("lines", [])) if "lines" in rec else rec.get("text", "")
    return set(re.findall(r"[a-z0-9$%.,]{3,}", t.lower()))

APP_ALIASES = {"vscode": "visual studio code", "code": "visual studio code", "vlc": "vlc", "zoomusinstaller": "zoom.us",
               "googlechrome": "google chrome", "chrome": "google chrome"}

def installer_app_present(name):
    """True if an installer's app (by first word / alias) is already in /Applications or ~/Applications."""
    stem = re.sub(r"\.(dmg|pkg|mpkg|iso)$", "", name, flags=re.I)
    stem = re.split(r"[-_ .]?(v?\d|darwin|arm64|x64|mac|universal|installer|setup|\()", stem, flags=re.I)[0]
    key = re.sub(r"[^a-z0-9 ]", " ", stem.lower()).strip()
    key = APP_ALIASES.get(key.replace(" ", ""), key)
    if not key:
        return False
    apps = []
    for d in ("/Applications", os.path.expanduser("~/Applications")):
        try: apps += [x[:-4].lower() for x in os.listdir(d) if x.endswith(".app")]
        except OSError: pass
    return any(a == key or a.startswith(key) or key.startswith(a) for a in apps if len(a) >= 3)

def load_sessions(path, root):
    """sessions.json: list of {topic, slug, start, end, [description], [folder]}; times are local ISO 8601."""
    from datetime import datetime
    out = []
    for se in json.load(open(path, encoding="utf-8")):
        se = dict(se)
        se["slug"] = se.get("slug") or slug(se["topic"], 4)
        se["_start"] = datetime.fromisoformat(se["start"]).timestamp()
        se["_end"] = datetime.fromisoformat(se["end"]).timestamp()
        f = se.get("folder")
        se["_dir"] = os.path.abspath(os.path.join(root, os.path.expanduser(f))) if f else os.path.join(root, "inspo", se["slug"])
        out.append(se)
    return out

DUP_DL = re.compile(r"^(.*?)(?: \(\d+\)| copy(?: \d+)?| - copy)(\.[^.]+)?$", re.I)

def cmd_plan(a):
    sd = os.path.abspath(a.scan)
    root = os.path.abspath(a.root)
    inv = list(jl_read(os.path.join(sd, "inventory.jsonl")))
    hashes = {r["path"]: r["sha256"] for r in jl_read(os.path.join(sd, "hashes.jsonl"))}
    ocr = {r["path"]: r for r in jl_read(os.path.join(sd, "ocr.jsonl"))}
    media = {r["path"]: r for r in jl_read(os.path.join(sd, "media.jsonl"))}
    overrides = {}
    if a.overrides and os.path.exists(a.overrides):  # TSV: old_path \t new_base_or_ACTION \t note
        for row in csv.reader(open(a.overrides, encoding="utf-8"), delimiter="\t"):
            if row and not row[0].startswith("#"):
                overrides[row[0]] = row[1:]
    rules = rules_from_args(a, root)
    private = {}
    for r in inv:
        why = r.get("private") or rules.check(r["path"])
        if why:
            private[r["path"]] = (why, r["size"])
    full_inv, inv = inv, [r for r in inv if r["path"] not in private]
    for p in private:  # forget anything read from private files
        ocr.pop(p, None); media.pop(p, None); hashes.pop(p, None)
    items = {r["path"]: dict(r, action="keep", confidence="", proposed=None, reason="") for r in inv}
    sessions = load_sessions(a.sessions, root) if a.sessions else []
    for it in items.values():
        if it["kind"] not in ("screenshot", "image") or CONVENTION.match(it["name"]):
            continue
        t = it.get("btime") or it["mtime"]
        for se in sessions:
            if se["_start"] <= t <= se["_end"]:
                it["session"] = se["slug"]; it["session_dir"] = se["_dir"]; it["session_topic"] = se["topic"]
                break

    # exact dups: keep newest
    groups = defaultdict(list)
    for p, h in hashes.items():
        if p in items: groups[h].append(p)
    for h, ps in groups.items():
        if len(ps) < 2: continue
        # keep the original-looking name ("x.pdf" over "x (1).pdf"), then the newest
        ps.sort(key=lambda p: (bool(DUP_DL.match(items[p]["name"])), -items[p]["mtime"]))
        for p in ps[1:]:
            if items[p].get("private"):
                items[p].update(reason=f"private; exact duplicate of {ps[0]} (not moved)", group=h[:12])
            else:
                items[p].update(action="to_delete", reason=f"exact duplicate of {ps[0]}", group=h[:12])
    # 'file (1).ext' style downloads whose base exists with same size
    names_by_dir = defaultdict(dict)
    for r in inv: names_by_dir[os.path.dirname(r["path"])][r["name"].lower()] = r
    for r in inv:
        it = items[r["path"]]
        m = DUP_DL.match(r["name"])
        if it["action"] == "keep" and m and not it.get("session"):
            base = (m.group(1) + (m.group(2) or "")).lower()
            o = names_by_dir[os.path.dirname(r["path"])].get(base)
            if o and o["size"] == r["size"] and items[o["path"]]["action"] == "keep":
                it.update(action="to_delete", reason=f"duplicate download of {o['name']} (same size)")
    # junk
    for it in items.values():
        if it["action"] != "keep" or it.get("session"): continue
        if it["ext"] in ("crdownload", "part", "download", "partial"):
            it.update(action="to_delete", reason="incomplete download")
        elif it["kind"] == "installer":
            installed = installer_app_present(it["name"])
            it.update(action="to_delete" if installed else "keep", reason="installer; app already installed" if installed else "installer (app not found in /Applications)")
        elif it["app_bundle"] and os.path.exists(os.path.join("/Applications", it["name"])) and not it["path"].startswith("/Applications/"):
            it.update(action="to_delete", reason="app bundle copy; same app already in /Applications")
        elif it["size"] == 0 and not it["app_bundle"]:
            it.update(action="to_delete", reason="empty file")
        elif it["kind"] in ("screenshot", "image") and it["path"] in ocr:
            rec = ocr[it["path"]]
            if not rec.get("lines") and it["size"] < 30_000 and it["kind"] == "screenshot":
                it.update(action="to_delete", reason="likely blank screenshot (no text, tiny)")
    # sensitive
    for it in items.values():
        rec = ocr.get(it["path"], {})
        txt = " ".join(l["t"] for l in rec.get("lines", [])) + rec.get("text", "")[:2000]
        if SENSITIVE_TEXT.search(txt) or SENSITIVE_NAME.search(it["name"]):
            it["sensitive"] = True
    # near-dup screenshots: same dir, within window, token containment
    shots = sorted([it for it in items.values() if it["kind"] == "screenshot" and it["path"] in ocr
                    and it["action"] == "keep" and not it.get("session")], key=lambda x: x["mtime"])
    toks = {it["path"]: tokens(ocr[it["path"]]) for it in shots}
    for i, x in enumerate(shots):
        for y in shots[i + 1:]:
            if y["mtime"] - x["mtime"] > a.near_window: break
            if os.path.dirname(x["path"]) != os.path.dirname(y["path"]): continue
            tx, ty = toks[x["path"]], toks[y["path"]]
            if len(tx) < 3 or len(ty) < 3: continue
            inter = len(tx & ty)
            for small, big, ts in ((x, y, tx), (y, x, ty)):
                if small["action"] == "keep" and inter / len(ts) >= a.near_threshold and len(ts) <= len(tx | ty):
                    small.update(action="to_delete", confidence="near_dup",
                                 reason=f"near-duplicate: {inter/len(ts):.0%} of text in {big['name']}")
                    break
    # names
    for it in items.values():
        if it["action"] != "keep": continue
        if it.get("private"):
            it.update(action="skip", reason="private folder: not named or moved"); continue
        name, kind = it["name"], it["kind"]
        stem = name.rsplit(".", 1)[0] if "." in name else name
        if "alias" in it["ext"] or (kind == "other" and it["depth"] > 0):
            it.update(action="skip", reason="alias/code/font or other non-document; left as is"); continue
        if it["depth"] > 0 and not (kind in ("screenshot", "image", "video", "screen_recording", "audio")
                                    and GENERIC_NAME.match(stem)) and not overrides.get(it["path"]):
            it.update(action="skip", reason="inside a sub-folder and already named; left as is"); continue
        if CONVENTION.match(name):
            it.update(action="skip", reason="already follows convention"); continue
        pre = PREFIX_FOR[kind]
        base, conf = "", ""
        ov = overrides.get(it["path"])
        if it.get("session") and not ov:
            topic = slug(it["session_topic"], 4)
            t = ""
            if it["path"] in ocr and "lines" in ocr[it["path"]]:
                t, _, _ = ocr_title(ocr[it["path"]])
                t = "-".join(w for w in t.split("-") if w not in topic.split("-"))
                t = "-".join(t.split("-")[:max(1, 6 - len(topic.split("-")))])
            base = pre + (topic + "-" + t if t else topic)
            it.update(confidence="session_text" if t else "session_needs_visual", proposed_base=base,
                      action="rename", reason=f"capture session: {it['session_topic']}")
            continue
        if ov:
            if ov[0].upper() == "TO_DELETE":
                it.update(action="to_delete", reason=ov[1] if len(ov) > 1 else "manual"); continue
            if ov[0].upper() == "KEEP":
                it.update(action="skip", reason="manual keep"); continue
            base, conf = ov[0], "manual"
        elif not GENERIC_NAME.match(stem) and kind != "screenshot":
            base, conf = pre + slug(stem), "from_filename"
        elif it["path"] in ocr:
            rec = ocr[it["path"]]
            if "lines" in rec:
                t, nlines, strong = ocr_title(rec)
            else:
                t = slug(rec.get("title") or clean_text(rec.get("text", "")[:1500]), 5); strong = bool(rec.get("title"))
            if t and t.count("-") >= 1:
                base, conf = pre + t, ("confident_text" if strong else "weak_text")
            else:
                base, conf = "", "needs_visual"
        elif it["path"] in media and media[it["path"]].get("transcript"):
            base, conf = pre + slug(media[it["path"]]["transcript"], 5), "weak_transcript"
        else:
            conf = "needs_visual" if kind in ("screenshot", "image", "video", "screen_recording") else "needs_review"
        if base and base == pre: base = ""
        it.update(confidence=conf, proposed_base=base or None, action="rename" if base else "keep_needs_name")
    # files whose text or name suggests passwords/keys/credentials go to a _sensitive folder (original name kept)
    for it in items.values():
        if it.get("sensitive"):
            it["proposed_base"] = None  # never put text from a sensitive file into a name or the plan
            if it["action"] in ("rename", "keep_needs_name", "to_delete"):
                it.update(action="sensitive", reason=(it.get("reason", "") + " sensitive text/name (password, key, credential)").strip())
            else:
                it["reason"] = (it.get("reason", "") + " [sensitive text/name; left in place]").strip()
    # destinations + clash resolution
    taken = set()
    for r in full_inv:  # existing names are taken
        taken.add(os.path.join(os.path.dirname(r["path"]), r["name"].lower()))
    def dest_dir(it):
        if it.get("session_dir"):
            return it["session_dir"]
        if a.layout == "categorize" and it["depth"] == 0:
            return os.path.join(root, CATEGORY_DIR[it["kind"]])
        return os.path.dirname(it["path"])
    for it in sorted(items.values(), key=lambda x: -x["mtime"]):
        if it["action"] == "rename":
            d = dest_dir(it); ext = ("." + it["ext"]) if it["ext"] and it["ext"] != "app" else (".app" if it["app_bundle"] else "")
            b = it["proposed_base"]; n = 2
            while os.path.join(d, (b + ext).lower()) in taken: b = f'{it["proposed_base"]}-{n}'; n += 1
            taken.add(os.path.join(d, (b + ext).lower()))
            it["new_path"] = os.path.join(d, b + ext)
        elif it["action"] in ("to_delete", "sensitive"):
            sub = "_to-delete" if it["action"] == "to_delete" else "_sensitive"
            d = os.path.join(dest_dir(it) if a.layout == "categorize" and it["depth"] == 0 else os.path.dirname(it["path"]), sub)
            it["new_path"] = os.path.join(d, it["name"])
        else:
            it["new_path"] = None
    from collections import Counter
    plan = {"root": root, "created": time.strftime("%Y-%m-%dT%H:%M:%S"), "layout": a.layout,
            "private": {"files": len(private), "bytes": sum(s for _, s in private.values()),
                        "by_rule": dict(Counter(w for w, _ in private.values()))},
            "items": list(items.values())}
    with open(a.out_plan, "w", encoding="utf-8") as fh:
        json.dump(plan, fh, ensure_ascii=False, indent=1)
    with open(a.out_csv, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["rel_path", "kind", "size_bytes", "action", "confidence", "new_name", "reason"])
        for it in plan["items"]:
            w.writerow([it["rel"], it["kind"], it["size"], it["action"], it.get("confidence", ""),
                        os.path.basename(it["new_path"]) if it.get("new_path") else "", it.get("reason", "")])
    summarize(plan)

def summarize(plan):
    from collections import Counter
    it = plan["items"]
    kinds = Counter(x["kind"] for x in it)
    size = defaultdict(int)
    for x in it: size[x["kind"]] += x["size"]
    print("files by kind:", {k: f"{v} ({size[k]/1e9:.2f} GB)" for k, v in kinds.most_common()})
    print("actions:", dict(Counter(x["action"] for x in it)))
    print("confidence:", dict(Counter(x.get("confidence") for x in it if x["action"] in ("rename", "keep_needs_name"))))
    def tot(f): s = [x for x in it if f(x)]; return len(s), sum(x["size"] for x in s)
    print("exact dups: %d files, %.2f GB" % (lambda n, s: (n, s / 1e9))(*tot(lambda x: x["action"] == "to_delete" and ("exact duplicate" in x.get("reason", "") or "duplicate download" in x.get("reason", "")))))
    print("near dups: %d files, %.2f MB" % (lambda n, s: (n, s / 1e6))(*tot(lambda x: x.get("confidence") == "near_dup")))
    print("junk: %d files, %.2f GB" % (lambda n, s: (n, s / 1e9))(*tot(lambda x: x["action"] == "to_delete" and any(k in x.get("reason", "") for k in ("installer", "blank", "empty", "incomplete", "app bundle")))))
    print("needs visual:", sum(1 for x in it if x["action"] in ("rename", "keep_needs_name") and x.get("confidence") in VISUAL_CONF))
    print("sensitive (-> _sensitive): %d files, %.2f MB" % (lambda n, s: (n, s / 1e6))(*tot(lambda x: x["action"] == "sensitive")))
    print("sensitive left in place (already-named files in sub-folders):", sum(1 for x in it if x.get("sensitive") and x["action"] != "sensitive"))
    pv = plan.get("private", {})
    print(f"private (excluded, count only): {pv.get('files', 0)} files, {pv.get('bytes', 0)/1e9:.2f} GB, by rule {pv.get('by_rule', {})}")

# ---------------------------------------------------------------- apply
def cmd_apply(a):
    plan = json.load(open(a.plan, encoding="utf-8"))
    root = plan["root"]
    log_dir = a.log_dir or os.path.join(root, "_to-delete")
    log_p = os.path.join(log_dir, "rename-log.csv")
    if not a.dry_run:
        os.makedirs(log_dir, exist_ok=True)
    new_log = not os.path.exists(log_p)
    rules = PrivacyRules(root)  # ignore.txt + .donotread + private-folders.txt
    ok = err = skipped = 0
    with (open(os.devnull, "w") if a.dry_run else open(log_p, "a", newline="", encoding="utf-8")) as fh:
        w = csv.writer(fh)
        if new_log: w.writerow(["old_path", "new_path"])
        for it in plan["items"]:
            if it["action"] not in ("rename", "to_delete", "sensitive") or not it.get("new_path"):
                skipped += 1; continue
            if a.only and it["action"] not in a.only:
                skipped += 1; continue
            src, dst = it["path"], it["new_path"]
            if it.get("private") or rules.check(src):
                log("REFUSED private file (not moved)"); skipped += 1; continue
            if not os.path.lexists(src):
                log("MISSING", src); err += 1; continue
            if os.path.lexists(dst):
                log("EXISTS", dst); err += 1; continue
            if a.dry_run:
                print(f"would mv -n {src!r} -> {dst!r}"); ok += 1; continue
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            r = subprocess.run(["mv", "-n", src, dst])
            if r.returncode == 0 and os.path.lexists(dst) and not os.path.lexists(src):
                w.writerow([src, dst]); fh.flush(); ok += 1
            else:
                log("FAIL", src); err += 1
    print(f"moved={ok} errors={err} skipped={skipped} log={log_p}")

# ---------------------------------------------------------------- init
def cmd_init(a):
    """Onboarding: ask what to ignore and write ~/.config/file-namer/ignore.txt (merged with existing entries)."""
    path = os.path.expanduser(a.ignore_file)
    existing = read_ignore_list(path)
    if existing:
        print(f"Currently ignored ({path}): {', '.join(existing)}")
    answer = a.answer if a.answer is not None else input(INIT_QUESTION + " (comma-separated, Enter for none)\n> ")
    new = [e.strip() for e in answer.split(",") if e.strip()]
    merged = list(dict.fromkeys(existing + [e.lower() for e in new]))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("# file-namer ignore list: folder names, file names or keywords (case-insensitive).\n"
                 "# Anything whose path contains one of these is treated as private: never read, previewed or moved.\n")
        for e in merged:
            fh.write(e + "\n")
    print(f"Saved {len(merged)} ignore entr{'y' if len(merged) == 1 else 'ies'} to {path}"
          + (f" (added: {', '.join(new)})" if new else ""))
    print(f"Tip: put an empty '{DONOTREAD}' file in any folder to make it and everything below it private.")

# ---------------------------------------------------------------- visual
VISUAL_CONF = ("needs_visual", "weak_text", "weak_transcript", "session_needs_visual")

def cmd_visual(a):
    """Make small previews (800px JPEG; middle frame for videos) for items that need a human/agent look.
    Refuses private files (privacy rules are re-checked here, independent of the plan) and sensitive ones."""
    plan = json.load(open(a.plan, encoding="utf-8"))
    root = plan["root"]
    rules = rules_from_args(a, root)
    conf = set(a.confidence or VISUAL_CONF)
    wanted = None
    if a.paths:
        wanted = {l.rstrip("\n") for l in open(a.paths, encoding="utf-8") if l.strip()}
    os.makedirs(a.out, exist_ok=True)
    idx_p = os.path.join(a.out, "index.tsv")
    made = refused_rule = 0
    with open(idx_p, "a", encoding="utf-8") as idx:
        for it in plan["items"]:
            if made >= a.limit:
                break
            if wanted is not None:
                if it["path"] not in wanted: continue
            elif it.get("confidence") not in conf or it["action"] not in ("rename", "keep_needs_name"):
                continue
            if it["kind"] not in ("screenshot", "image", "video", "screen_recording"):
                continue
            if it.get("private") or it.get("sensitive") or rules.check(it["path"]):  # hard stop
                refused_rule += 1; continue
            key = hashlib.md5(it["path"].encode()).hexdigest()[:10]
            outp = os.path.join(a.out, key + ".jpg")
            tmp = outp + ".tmp.jpg"
            if it["kind"] in ("video", "screen_recording"):
                mid = 0
                try:
                    mid = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of",
                                                "csv=p=0", it["path"]], capture_output=True, text=True, timeout=60).stdout or 0) / 2
                except Exception:
                    pass
                subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{mid:.2f}", "-i", it["path"], "-frames:v", "1",
                                "-vf", "scale=800:-2", "-q:v", "5", tmp], timeout=120)
            else:
                subprocess.run(["sips", "-s", "format", "jpeg", "-s", "formatOptions", "60", "--resampleWidth", "800",
                                it["path"], "--out", tmp], capture_output=True, timeout=120)
            if not os.path.exists(tmp):
                continue
            os.replace(tmp, outp)
            idx.write(f"{key}.jpg\t{it['path']}\t{it.get('confidence','')}\t{os.path.basename(it.get('new_path') or '')}\n")
            made += 1
    print(f"previews={made} refused_private={refused_rule} index={idx_p}")

# ---------------------------------------------------------------- cli
def main():
    ap = argparse.ArgumentParser(description=__doc__)
    sp = ap.add_subparsers(dest="cmd", required=True)
    s = sp.add_parser("scan"); s.add_argument("root"); s.add_argument("--out", required=True)
    s.add_argument("--recursive", action="store_true"); s.add_argument("--exclude", action="append")
    s.add_argument("--no-hash", action="store_true"); s.add_argument("--ocr", action="store_true")
    s.add_argument("--ocr-all", action="store_true", help="OCR every image, not only generic-named ones")
    s.add_argument("--helper"); s.add_argument("--private-re", help="regex on relative path: private (counted only)"); s.add_argument("--media", action="store_true")
    s.add_argument("--frames", action="store_true"); s.add_argument("--whisper-model")
    s.add_argument("--private-dir", action="append", help="folder to treat as private (repeatable)")
    p = sp.add_parser("plan"); p.add_argument("root"); p.add_argument("--scan", required=True)
    p.add_argument("--out-plan", required=True); p.add_argument("--out-csv", required=True)
    p.add_argument("--overrides"); p.add_argument("--private-re", help="regex on relative path: private (counted only)"); p.add_argument("--sessions", help="sessions.json capture windows"); p.add_argument("--layout", choices=["categorize", "in_place"], default="categorize")
    p.add_argument("--near-window", type=int, default=600); p.add_argument("--near-threshold", type=float, default=0.85)
    p.add_argument("--private-dir", action="append")
    v = sp.add_parser("visual", help="make previews for needs-visual items (refuses private files)")
    v.add_argument("plan"); v.add_argument("--out", required=True); v.add_argument("--limit", type=int, default=25)
    v.add_argument("--confidence", action="append", help="which confidences to preview (default: needs_visual/weak)")
    v.add_argument("--paths", help="file with exact paths to preview instead")
    v.add_argument("--private-re"); v.add_argument("--private-dir", action="append")
    i = sp.add_parser("init", help="onboarding: ask which folders/files/keywords to ignore")
    i.add_argument("--answer", help="non-interactive answer (comma-separated)")
    i.add_argument("--ignore-file", default=USER_IGNORE_FILE)
    x = sp.add_parser("apply"); x.add_argument("plan"); x.add_argument("--log-dir")
    x.add_argument("--dry-run", action="store_true"); x.add_argument("--only", action="append", choices=["rename", "to_delete", "sensitive"])
    a = ap.parse_args()
    {"init": cmd_init, "scan": cmd_scan, "plan": cmd_plan, "visual": cmd_visual, "apply": cmd_apply}[a.cmd](a)

if __name__ == "__main__":
    main()
