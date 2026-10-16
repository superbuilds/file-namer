# file-namer

Renames and sorts messy macOS files (screenshots, screen recordings, downloads) into a consistent naming
convention and a tidy folder layout. Every run is a dry-run plan first. It **never deletes and never overwrites**:
each change is an `mv -n`, recorded in `rename-log.csv`, and anything that looks disposable goes into a
`_to-delete/` folder for you to empty yourself.

Everything runs locally: Apple Vision OCR, PDFKit, `textutil`, `ffprobe`/`ffmpeg`, and optionally `whisper-cpp`.

## Naming convention
`<prefix><2-6 hyphenated lowercase words>.<ext>`. No dates; name clashes get `-2`, `-3`.

| prefix | for | folder (loose top-level files) |
|---|---|---|
| `ss_` | screenshots | `screenshots/` |
| `vid_` | videos, screen recordings | `videos/` |
| `img_` | photos/images | `images/` |
| `doc_` | documents, data, PDFs | `docs/` |
| `aud_` | audio | `audio/` |
| `zip_` | archives | `archives/` |
| `app_` | installers, `.app` bundles | `installers/` |
| `misc_` | anything else | `other/` |

Each of those folders gets its own `_to-delete/` (and `_sensitive/`, see below) when needed. Files already
matching the convention (lowercase prefix + hyphenated words) are left alone; `IMG_1234.jpg` is not a match.

## Requirements
- macOS with `python3` (standard library only) and Xcode Command Line Tools (`swiftc`).
- Optional: `ffmpeg` (video metadata and middle frames), `whisper-cpp` + a small model such as
  `ggml-base.en.bin` (~148 MB) for a 20-second speech snippet from the middle of each video.

```
git clone https://github.com/superbuilds/file-namer ~/.file-namer   # or download the tarball
cd ~/.file-namer && swiftc -O ocr_helper.swift -o ocr_helper
```
`ocr_helper` reads one path per line on stdin and writes one JSON line per file (Vision OCR for images, PDFKit
text for PDFs). It runs as a single long-lived process and handles one file at a time.

## Onboarding (do this first)
1. **Ask: "Are there any folder names, file names, or keywords you want us to ignore?"**
   ```
   python3 filenamer.py init                                  # asks interactively
   python3 filenamer.py init --answer "Personal, taxes, k"    # non-interactive
   ```
   Answers (comma-separated) are merged into `~/.config/file-namer/ignore.txt`, one per line, lowercased.
   Edit it by hand any time; `#` starts a comment; re-running `init` adds to the list.
2. Optionally put an empty **`.donotread`** file in any folder (`touch ~/Somewhere/.donotread`): that folder
   and everything below it are private.
3. Optionally list more private folders in `~/.config/file-namer/private-folders.txt` (one per line).
4. Then `scan` → `plan` → (`visual`) → `apply`.

## Privacy
A file is **private** if any of these match:
1. a `.donotread` file in its folder or any parent folder;
2. `~/.config/file-namer/ignore.txt` (from `init`): case-insensitive, matched against every folder name and the
   file name. Entries of 3+ characters match at word boundaries (`taxes` matches `Taxes 2023/` and
   `old-taxes.pdf`, `tax` does not match `syntax`); entries of 1-2 characters must equal a whole folder/file name
   (with or without extension), so `k` matches a folder `k` but not `IMG_4K.jpg`. Avoid entries that match your
   home path (such as your username), or everything becomes private;
3. `~/.config/file-namer/private-folders.txt` and `--private-dir DIR` (absolute, `~/...`, or relative to the root);
4. `--private-re REGEX` on the path relative to the root.

Private files are counted (number and size) and nothing else: never hashed into duplicate groups, OCR'd, read,
framed, transcribed, previewed, named or moved. Plans and CSV previews leave them out and report only
`private: {files, bytes, by_rule}`. `visual` and `apply` re-check the rules themselves, so an old plan cannot
preview or move a private file.

**Sensitive text** is separate: files whose OCR/text or name suggests passwords, API keys, private keys, tokens or
recovery phrases are moved, under their original name, into a `_sensitive/` folder next to where they would have
gone, so you can review them. They are never previewed by `visual`. Already-named files inside your own sub-folders
are only flagged, not moved.

## Phases
### 1. scan (read-only)
```
python3 filenamer.py scan ROOT --out SCAN_DIR [--recursive] [--ocr] [--ocr-all] \
    [--media] [--frames] [--whisper-model PATH] [--exclude DIR ...] [--private-dir DIR ...] [--private-re REGEX]
```
Appends JSON lines to SCAN_DIR as it goes, so a scan can be stopped and resumed:
- `inventory.jsonl`: path, size, times, extension, kind, privacy reason
- `hashes.jsonl`: sha256, only for files whose size collides with another file (cheap exact-duplicate check)
- `ocr.jsonl`: OCR lines for screenshots and generic-named images (all images with `--ocr-all`), PDF text, the
  first ~2 KB of text documents (txt/md/csv/json/vtt, docx/rtf/html via `textutil`, xlsx strings), Spotlight title
- `media.jsonl`: duration, resolution, audio (ffprobe); `--frames` saves an 800px middle frame;
  `--whisper-model` adds a 20 s transcript from the middle

Always skipped: hidden files and folders, anything inside a folder containing `.git`, `Library`, `System`,
`Applications`, `node_modules`, virtualenvs, `*_files` (saved web pages), and `--exclude` folders. `.app` bundles
count as one item.

### 2. plan (read-only)
```
python3 filenamer.py plan ROOT --scan SCAN_DIR --out-plan plan.json --out-csv preview.csv \
    [--overrides overrides.tsv] [--sessions sessions.json] [--layout categorize|in_place] \
    [--near-window 600] [--near-threshold 0.85]
```
- **exact duplicates** (same sha256): keep the original-looking, newest copy; others → `_to-delete`
- **duplicate downloads** (`name (1).ext`, `copy`) with the same size as the original → `_to-delete`
- **junk**: empty files, partial downloads, installers and `.app` copies whose app is already installed,
  tiny screenshots with no text → `_to-delete`
- **near-duplicate screenshots**: same folder, taken within `--near-window` seconds, and at least
  `--near-threshold` of one's OCR words appear in the other → the subset one goes to `_to-delete`
- **names**: descriptive existing names are normalised (prefix + slug); generic names (Screenshot…, IMG_…,
  uuid, untitled…) are named from OCR / PDF / document text or the speech transcript
- **confidence**: `manual`, `from_filename`, `confident_text` (a prominent OCR title line with 3+ real words),
  `weak_text`, `weak_transcript`, `session_text`, `session_needs_visual`, `needs_visual`, `needs_review`, `near_dup`
- **layout** `categorize` (default): loose top-level files move into the folders in the table above. Inside your
  own sub-folders only generic-named media is renamed, in place; already-named documents, exports, code, fonts
  and Finder aliases there are left untouched. `in_place`: rename everything where it is.

**Overrides** (`overrides.tsv`, tab-separated `old_path<TAB>new_base_or_ACTION<TAB>note`) let a person or an
agent that looked at the `needs_visual` items supply names: `ss_some-better-name`, `TO_DELETE<TAB>reason`, or `KEEP`.

### Capture sessions (`--sessions sessions.json`)
When you take a burst of screenshots on purpose (ad inspiration, a bug repro, a competitor teardown), declare the
time window so they are treated as a set instead of clutter:
```json
[
  {"topic": "Pool ad blue tile", "slug": "pool-ad", "start": "2026-10-15T14:00", "end": "2026-10-15T14:45",
   "description": "hero image ideas for the spring campaign"},
  {"topic": "Checkout bug repro", "slug": "checkout-bug", "start": "2026-09-27T16:00",
   "end": "2026-09-27T16:40", "folder": "~/Desktop/checkout-bug"}
]
```
- Times are local ISO 8601. A screenshot/image belongs to a session if its creation time (falling back to
  modification time) is inside `[start, end]`.
- Destination: `ROOT/inspo/<slug>/`, or `folder` if given (absolute, `~/...`, or relative to ROOT).
- Names are topic-aware: `ss_<topic words>-<OCR title words>`, e.g. `ss_pool-ad-blue-tile-hero.png`; without
  usable text: `ss_pool-ad-blue-tile.png`, `-2`, `-3` (confidence `session_needs_visual`).
- Session items are exempt from junk, near-duplicate and "(1)" duplicate-download flagging; byte-identical copies
  are still flagged. `overrides.tsv` still wins.

### 3. visual (optional)
```
python3 filenamer.py visual plan.json --out PREVIEW_DIR [--limit 25] [--paths list.txt] [--confidence needs_visual]
```
Writes 800px JPEG previews (middle frame for videos) and `index.tsv` (preview, path, confidence, proposed name)
for `needs_visual` / `weak_*` items, so someone can look and write `overrides.tsv`. Refuses private and sensitive
files. Each look costs time (and, for an AI agent, credits): agree on a batch size before previewing many files.

### 4. apply
```
python3 filenamer.py apply plan.json [--dry-run] [--only rename|to_delete|sensitive] [--log-dir DIR]
```
Moves `rename`, `to_delete` and `sensitive` items with `mv -n`; checks that the source exists and the destination
does not, and verifies afterwards. Every successful move is appended to `<log-dir>/rename-log.csv`
(`old_path,new_path`; default `ROOT/_to-delete/`). To revert, move each `new_path` back to `old_path`.

## Typical run
```
python3 filenamer.py init
python3 filenamer.py scan ~/Desktop --out /tmp/fn-scan --ocr --media
python3 filenamer.py plan ~/Desktop --scan /tmp/fn-scan --out-plan /tmp/plan.json --out-csv /tmp/preview.csv
# review preview.csv; optionally preview a few needs-visual items:
python3 filenamer.py visual /tmp/plan.json --out /tmp/fn-previews --limit 25
# write overrides.tsv, re-run plan with --overrides, then:
python3 filenamer.py apply /tmp/plan.json --dry-run
python3 filenamer.py apply /tmp/plan.json
```
Memory stays flat: the walk, hashing (1 MB chunks), OCR and text extraction stream one file at a time and append
results to disk.

## License
MIT, see `LICENSE`.
