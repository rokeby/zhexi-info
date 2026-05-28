#!/usr/bin/env python3
"""
site_cms.py — Local CMS for the zhexi.info timeline.

Edits entries.yaml (single source of truth), saves timestamped backups,
and triggers build_site.py to regenerate index.html.

Features:
  - browse / search / add / edit / delete entries
  - multiple images per entry, with drag-and-drop upload (saved to images/)
  - research-theme management
  - bulk selection + bulk edits (add/remove/set types, add/remove themes, delete)
  - "build site" button (runs build_site.py)

Usage:
    .venv/bin/python site_cms.py                  # serves on http://localhost:5111
    .venv/bin/python site_cms.py --port 8080      # custom port
"""

import argparse
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

try:
    import yaml
except ImportError:
    sys.exit("PyYAML not found. Install it: pip install pyyaml")

try:
    from flask import Flask, jsonify, request, Response, send_from_directory
    from werkzeug.utils import secure_filename
except ImportError:
    sys.exit("Flask not found. Install it: pip install flask")

# ─── Config ───────────────────────────────────────────────────────────────────

DEFAULT_DATA = Path("entries.yaml")
BACKUP_DIR = Path(".site_backups")
IMAGES_DIR = Path("images")
ALLOWED_TYPES = ["exhibition", "writing", "research", "talk", "teaching"]
BUILD_SCRIPT = Path("build_site.py")
ALLOWED_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif", ".svg"}


# ─── Image helpers ───────────────────────────────────────────────────────────

# Maps many month spellings/numbers to the canonical abbreviation used on the site.
MONTH_ALIASES = {
    "1": "jan", "01": "jan", "jan": "jan", "january": "jan",
    "2": "feb", "02": "feb", "feb": "feb", "february": "feb",
    "3": "mar", "03": "mar", "mar": "mar", "march": "mar",
    "4": "apr", "04": "apr", "apr": "apr", "april": "apr",
    "5": "may", "05": "may", "may": "may",
    "6": "jun", "06": "jun", "jun": "jun", "june": "jun",
    "7": "jul", "07": "jul", "jul": "jul", "july": "jul",
    "8": "aug", "08": "aug", "aug": "aug", "august": "aug",
    "9": "sept", "09": "sept", "sep": "sept", "sept": "sept", "september": "sept",
    "10": "oct", "oct": "oct", "october": "oct",
    "11": "nov", "nov": "nov", "november": "nov",
    "12": "dec", "dec": "dec", "december": "dec",
}


def normalize_display_date(s: str) -> str:
    """Tidy common 'month year' inputs into a canonical 'sept 2025' form.

    Leaves anything that isn't a clean single month+year untouched — so
    ranges ('apr-jul 2025'), spans ('2022-23'), year-only ('2025'), and free
    text ('upcoming') pass through unchanged.
    """
    s = (s or "").strip()
    if not s:
        return s

    # "<month> <year>", "<month>/<year>", "<month>.<year>" (no hyphen → ranges survive)
    m = re.match(r"^([A-Za-z]+|\d{1,2})[\s/.]+(\d{4})$", s)
    if m:
        tok = m.group(1).lower().rstrip(".")
        if tok in MONTH_ALIASES:
            return f"{MONTH_ALIASES[tok]} {m.group(2)}"
        return s

    # Bare alphabetic month name (no year) → canonical abbreviation.
    tok = s.lower().rstrip(".")
    if tok.isalpha() and tok in MONTH_ALIASES:
        return MONTH_ALIASES[tok]

    return s


MONTH_NUM = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
             "jul": 7, "aug": 8, "sept": 9, "oct": 10, "nov": 11, "dec": 12}


def derive_sort(display: str) -> str | None:
    """Derive a YYYY-MM-15 sort key from a display date string.

    "jun 2025" -> 2025-06-15 ; "9/2025" -> 2025-09-15 ; "2025" -> 2025-06-15.
    Ranges/spans ("apr-jul 2025", "2025-2027") fall back to <year>-06-15 using
    the first year found. Returns None if no 4-digit year is present.
    """
    s = (display or "").strip().lower()
    if not s:
        return None
    ym = re.search(r"\b(?:19|20)\d{2}\b", s)
    if not ym:
        return None
    year = ym.group(0)
    month = None
    m = re.match(r"^([a-z]+|\d{1,2})[\s/.]", s)
    if m and m.group(1) in MONTH_ALIASES:
        month = MONTH_NUM[MONTH_ALIASES[m.group(1)]]
    return f"{year}-{month:02d}-15" if month else f"{year}-06-15"


def normalize_images(raw) -> list:
    """Coerce an images value into a list of {src, alt} dicts."""
    out = []
    for im in raw or []:
        if isinstance(im, str) and im.strip():
            out.append({"src": im.strip(), "alt": "", "caption": ""})
        elif isinstance(im, dict) and (im.get("src") or "").strip():
            out.append({
                "src": im["src"].strip(),
                "alt": (im.get("alt") or "").strip(),
                "caption": (im.get("caption") or "").strip(),
            })
    return out


# ─── YAML I/O ────────────────────────────────────────────────────────────────

def now_ts() -> str:
    return datetime.now().isoformat(timespec="seconds")


def load_data() -> dict:
    if not DEFAULT_DATA.exists():
        return {"site": {}, "themes": [], "entries": []}
    data = yaml.safe_load(DEFAULT_DATA.read_text(encoding="utf-8")) or {}
    data.setdefault("site", {})
    data.setdefault("themes", [])
    data.setdefault("entries", [])
    # Site module defaults (so the editor always has fields + toggles).
    site = data["site"]
    site.setdefault("news", [])
    site.setdefault("affiliations", [])
    site.setdefault("links", [])
    site.setdefault("show", {})
    for k in ("heading", "news", "bio", "affiliations", "links"):
        site["show"].setdefault(k, True)
    # Gallery (independent artworks for the carousel).
    data["gallery"] = [
        {"src": (g.get("src") or "").strip(),
         "caption": (g.get("caption") or "").strip(),
         "alt": (g.get("alt") or "").strip()}
        for g in (data.get("gallery") or []) if isinstance(g, dict) and (g.get("src") or "").strip()
    ]
    data.setdefault("gallery_shuffle", False)
    # Normalize entries: images, and visibility/timestamp defaults.
    for e in data["entries"]:
        if e.get("images") is not None:
            e["images"] = normalize_images(e.get("images"))
        elif e.get("image"):
            e["images"] = [{"src": e["image"], "alt": e.get("image_alt") or ""}]
        else:
            e["images"] = []
        e.pop("image", None)
        e.pop("image_alt", None)
        if "visible" not in e:
            e["visible"] = True
        # Coerce date-like fields to ISO strings. Unquoted YAML dates parse as
        # date objects, which jsonify renders as RFC-822 — breaking sort order.
        for k in ("sort", "created", "modified"):
            v = e.get(k)
            if v is not None and not isinstance(v, str):
                e[k] = v.isoformat() if hasattr(v, "isoformat") else str(v)
    return data


def save_data(data: dict) -> None:
    """Write YAML with timestamped backup."""
    BACKUP_DIR.mkdir(exist_ok=True)
    if DEFAULT_DATA.exists():
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        shutil.copy(DEFAULT_DATA, BACKUP_DIR / f"entries.{ts}.yaml")

    class _Dumper(yaml.SafeDumper):
        pass

    def _list_repr(dumper, data):
        # Compact flow style for short lists of plain strings (types/themes).
        if data and all(isinstance(x, str) for x in data) and len(data) <= 6:
            return dumper.represent_sequence("tag:yaml.org,2002:seq", data, flow_style=True)
        return dumper.represent_sequence("tag:yaml.org,2002:seq", data, flow_style=False)

    _Dumper.add_representer(list, _list_repr)

    yaml_text = yaml.dump(
        data,
        Dumper=_Dumper,
        sort_keys=False,
        allow_unicode=True,
        width=10_000,
        default_flow_style=False,
    )
    DEFAULT_DATA.write_text(yaml_text, encoding="utf-8")


def next_entry_id(data: dict) -> int:
    existing = [e.get("id", 0) for e in data.get("entries", []) if isinstance(e.get("id"), int)]
    return max(existing, default=0) + 1


# ─── Validation ──────────────────────────────────────────────────────────────

def validate_entry(entry: dict, data: dict) -> str | None:
    if not entry.get("date"):
        return "date is required"
    if not entry.get("sort"):
        return "sort date is required"
    if not entry.get("description_html"):
        return "description_html is required"
    types = entry.get("types") or []
    if not types:
        return "at least one type is required"
    for t in types:
        if t not in ALLOWED_TYPES:
            return f"unknown type '{t}'"
    theme_slugs = {t["slug"] for t in data.get("themes", [])}
    for th in entry.get("themes") or []:
        if th not in theme_slugs:
            return f"theme '{th}' not defined"
    return None


def entry_from_payload(payload: dict, eid: int, existing: dict | None = None) -> dict:
    now = now_ts()
    created = (existing or {}).get("created") or now
    date = normalize_display_date(payload.get("date") or "")
    sort = (payload.get("sort") or "").strip()
    if not sort:
        sort = derive_sort(date) or ""
    return {
        "id": eid,
        "date": date,
        "sort": sort,
        "visible": bool(payload.get("visible", True)),
        "types": payload.get("types") or [],
        "themes": payload.get("themes") or [],
        "description_html": (payload.get("description_html") or "").strip(),
        "images": normalize_images(payload.get("images")),
        "created": created,
        "modified": now,
    }


# ─── Flask app ───────────────────────────────────────────────────────────────

app = Flask(__name__)


@app.route("/")
def index():
    return Response(INDEX_HTML, mimetype="text/html")


@app.route("/api/data")
def api_data():
    return jsonify(load_data())


@app.route("/images/<path:filename>")
def serve_image(filename):
    return send_from_directory(IMAGES_DIR.resolve(), filename)


@app.route("/api/upload", methods=["POST"])
def api_upload():
    IMAGES_DIR.mkdir(exist_ok=True)
    saved, skipped = [], []
    for f in request.files.getlist("files"):
        if not f.filename:
            continue
        name = secure_filename(f.filename)
        ext = Path(name).suffix.lower()
        if ext not in ALLOWED_IMAGE_EXTS:
            skipped.append(f.filename)
            continue
        # Avoid collisions: append -1, -2, … if needed.
        dest = IMAGES_DIR / name
        stem, suffix = Path(name).stem, Path(name).suffix
        n = 1
        while dest.exists():
            dest = IMAGES_DIR / f"{stem}-{n}{suffix}"
            n += 1
        f.save(dest)
        saved.append(f"images/{dest.name}")
    return jsonify({"paths": saved, "skipped": skipped})


@app.route("/api/entries", methods=["POST"])
def api_create_entry():
    data = load_data()
    entry = entry_from_payload(request.get_json(), next_entry_id(data))
    err = validate_entry(entry, data)
    if err:
        return jsonify({"error": err}), 400
    data["entries"].append(entry)
    save_data(data)
    return jsonify(entry), 201


@app.route("/api/entries/<int:eid>", methods=["PUT"])
def api_update_entry(eid):
    data = load_data()
    for i, e in enumerate(data["entries"]):
        if e.get("id") == eid:
            updated = entry_from_payload(request.get_json(), eid, existing=e)
            err = validate_entry(updated, data)
            if err:
                return jsonify({"error": err}), 400
            data["entries"][i] = updated
            save_data(data)
            return jsonify(updated)
    return jsonify({"error": f"Entry {eid} not found"}), 404


@app.route("/api/entries/<int:eid>", methods=["DELETE"])
def api_delete_entry(eid):
    data = load_data()
    before = len(data["entries"])
    data["entries"] = [e for e in data["entries"] if e.get("id") != eid]
    if len(data["entries"]) == before:
        return jsonify({"error": f"Entry {eid} not found"}), 404
    save_data(data)
    return jsonify({"ok": True})


@app.route("/api/entries/<int:eid>/visibility", methods=["POST"])
def api_set_visibility(eid):
    visible = bool(request.get_json().get("visible", True))
    data = load_data()
    for e in data["entries"]:
        if e.get("id") == eid:
            e["visible"] = visible
            e["modified"] = now_ts()
            save_data(data)
            return jsonify({"ok": True, "visible": visible})
    return jsonify({"error": f"Entry {eid} not found"}), 404


@app.route("/api/site", methods=["POST"])
def api_save_site():
    payload = request.get_json()
    data = load_data()
    site = data.get("site") or {}
    site["heading_html"] = (payload.get("heading_html") or "").strip()
    site["news"] = [n.strip() for n in (payload.get("news") or []) if (n or "").strip()]
    site["bio_html"] = (payload.get("bio_html") or "").strip()
    site["affiliations"] = [x.strip() for x in (payload.get("affiliations") or []) if (x or "").strip()]
    site["links"] = [x.strip() for x in (payload.get("links") or []) if (x or "").strip()]
    show = payload.get("show") or {}
    site["show"] = {k: bool(show.get(k, True)) for k in ("heading", "news", "bio", "affiliations", "links")}
    data["site"] = site
    save_data(data)
    return jsonify(site)


@app.route("/api/gallery", methods=["POST"])
def api_save_gallery():
    payload = request.get_json()
    items = []
    for g in payload.get("gallery") or []:
        src = (g.get("src") or "").strip()
        if src:
            items.append({"src": src, "caption": (g.get("caption") or "").strip(), "alt": (g.get("alt") or "").strip()})
    data = load_data()
    data["gallery"] = items
    data["gallery_shuffle"] = bool(payload.get("shuffle"))
    save_data(data)
    return jsonify({"ok": True, "count": len(items)})


@app.route("/api/bulk", methods=["POST"])
def api_bulk():
    payload = request.get_json()
    ids = set(payload.get("ids") or [])
    if not ids:
        return jsonify({"error": "no ids selected"}), 400
    data = load_data()

    # Bulk delete
    if payload.get("delete"):
        before = len(data["entries"])
        data["entries"] = [e for e in data["entries"] if e.get("id") not in ids]
        save_data(data)
        return jsonify({"ok": True, "deleted": before - len(data["entries"])})

    # Bulk show / hide
    if "set_visible" in payload:
        val = bool(payload["set_visible"])
        now = now_ts()
        affected = 0
        for e in data["entries"]:
            if e.get("id") in ids:
                e["visible"] = val
                e["modified"] = now
                affected += 1
        save_data(data)
        return jsonify({"ok": True, "affected": affected})

    # Bulk field operations
    ops = payload.get("operations") or []
    theme_slugs = {t["slug"] for t in data.get("themes", [])}
    affected = 0
    for e in data["entries"]:
        if e.get("id") not in ids:
            continue
        for op in ops:
            field = op.get("field")
            action = op.get("action")
            values = op.get("values") or []
            if field == "types":
                values = [v for v in values if v in ALLOWED_TYPES]
            elif field == "themes":
                values = [v for v in values if v in theme_slugs]
            else:
                continue
            current = list(e.get(field) or [])
            if action == "add":
                for v in values:
                    if v not in current:
                        current.append(v)
            elif action == "remove":
                current = [v for v in current if v not in values]
            elif action == "set":
                current = list(values)
            else:
                continue
            # Never leave an entry with zero types.
            if field == "types" and not current:
                continue
            e[field] = current
        affected += 1
    save_data(data)
    return jsonify({"ok": True, "affected": affected})


@app.route("/api/themes", methods=["POST"])
def api_create_theme():
    payload = request.get_json()
    slug = (payload.get("slug") or "").strip().lower()
    label = (payload.get("label") or slug).strip()
    if not slug:
        return jsonify({"error": "slug required"}), 400
    data = load_data()
    if any(t.get("slug") == slug for t in data["themes"]):
        return jsonify({"error": f"theme '{slug}' already exists"}), 400
    data["themes"].append({"slug": slug, "label": label})
    save_data(data)
    return jsonify({"slug": slug, "label": label}), 201


@app.route("/api/themes/<slug>", methods=["PUT"])
def api_update_theme(slug):
    payload = request.get_json()
    new_slug = (payload.get("slug") or slug).strip().lower()
    new_label = (payload.get("label") or new_slug).strip()
    if not new_slug:
        return jsonify({"error": "slug required"}), 400
    data = load_data()
    theme = next((t for t in data["themes"] if t.get("slug") == slug), None)
    if not theme:
        return jsonify({"error": f"theme '{slug}' not found"}), 404
    if new_slug != slug and any(t.get("slug") == new_slug for t in data["themes"]):
        return jsonify({"error": f"theme '{new_slug}' already exists"}), 400
    theme["slug"] = new_slug
    theme["label"] = new_label
    if new_slug != slug:
        # Re-point every entry that referenced the old slug.
        for e in data["entries"]:
            if slug in (e.get("themes") or []):
                e["themes"] = [new_slug if x == slug else x for x in e["themes"]]
    save_data(data)
    return jsonify({"slug": new_slug, "label": new_label})


@app.route("/api/themes/<slug>", methods=["DELETE"])
def api_delete_theme(slug):
    data = load_data()
    before = len(data["themes"])
    data["themes"] = [t for t in data["themes"] if t.get("slug") != slug]
    if len(data["themes"]) == before:
        return jsonify({"error": f"theme '{slug}' not found"}), 404
    for e in data["entries"]:
        if slug in (e.get("themes") or []):
            e["themes"] = [t for t in e["themes"] if t != slug]
    save_data(data)
    return jsonify({"ok": True})


@app.route("/api/build", methods=["POST"])
def api_build():
    if not BUILD_SCRIPT.exists():
        return jsonify({"error": f"{BUILD_SCRIPT} not found"}), 500
    result = subprocess.run(
        [sys.executable, str(BUILD_SCRIPT)],
        capture_output=True,
        text=True,
    )
    return jsonify({
        "ok": result.returncode == 0,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "returncode": result.returncode,
    })


# ─── Embedded UI ─────────────────────────────────────────────────────────────

INDEX_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>zhexi.info CMS</title>
  <style>
    :root {
      --bg: #fafaf7; --fg: #111; --muted: #888; --accent: #c33; --border: #ddd; --hover: #f0eee8;
    }
    body { font-family: ui-sans-serif, system-ui, -apple-system, sans-serif; margin: 0; background: var(--bg); color: var(--fg); }
    header { padding: 1em 1.5em; border-bottom: 1px solid var(--border); display: flex; gap: 1em; align-items: center; flex-wrap: wrap; background: white; position: sticky; top: 0; z-index: 20; }
    header h1 { margin: 0; font-size: 1.1em; font-weight: 600; }
    header .spacer { flex: 1; }
    button { font: inherit; padding: 0.4em 0.9em; border: 1px solid var(--border); background: white; border-radius: 4px; cursor: pointer; }
    button:hover { background: var(--hover); }
    button.primary { background: var(--accent); color: white; border-color: var(--accent); }
    button.primary:hover { background: #a22; }
    button.danger { color: var(--accent); }
    button.tiny { padding: 0.2em 0.5em; font-size: 0.85em; }
    input[type="text"], input[type="date"], textarea, select { font: inherit; padding: 0.4em 0.6em; border: 1px solid var(--border); border-radius: 4px; box-sizing: border-box; width: 100%; }
    textarea { min-height: 6em; font-family: ui-monospace, monospace; font-size: 0.9em; resize: vertical; }
    label { display: block; font-size: 0.85em; color: var(--muted); margin-bottom: 0.2em; margin-top: 0.7em; }
    .toolbar { padding: 0.7em 1.5em; display: flex; gap: 1em; align-items: center; border-bottom: 1px solid var(--border); background: white; flex-wrap: wrap; position: sticky; top: 61px; z-index: 19; }
    .toolbar input[type="text"] { width: 280px; }
    .pill { display: inline-block; padding: 0.2em 0.45em; border-radius: 4px; font-size: 0.72em; line-height: 1; background: #eee; color: #555; margin-right: 0.3em; }
    .pill.type { background: #eee; color: #555; }
    .pill.type-exhibition { background: #e3f0ff; color: #14467f; }
    .pill.type-writing    { background: #e7f5ea; color: #1f6b35; }
    .pill.type-research   { background: #f1e7fb; color: #5b2d8c; }
    .pill.type-talk       { background: #fff0df; color: #95490a; }
    .pill.type-teaching   { background: #fde7ef; color: #9c2b5e; }
    .pill.theme { background: #def; color: #136; }
    main { padding: 1.5em; max-width: 1100px; margin: 0 auto; }
    .entry { padding: 0.6em 1em; border: 1px solid var(--border); border-radius: 6px; background: white; margin-bottom: 0.5em; display: grid; grid-template-columns: auto 6em 8em minmax(0, 1fr) auto; gap: 0.7em; align-items: center; }
    .entry:hover { background: var(--hover); }
    .entry.selected { border-color: var(--accent); box-shadow: 0 0 0 1px var(--accent); }
    .entry .sel { width: auto; }
    .entry .body { cursor: pointer; min-width: 0; }
    .entry .date { font-weight: 600; color: var(--fg); }
    .entry .types { font-size: 0.85em; }
    .entry .desc { font-size: 0.9em; color: #444; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; min-width: 0; }
    .entry .types { display: flex; flex-wrap: wrap; gap: 0.25em; align-content: center; }
    .entry .types .pill { margin-right: 0; }
    .entry .meta-right { display: flex; gap: 0.3em; align-items: center; justify-content: flex-end; flex-wrap: wrap; max-width: 15em; }
    .entry .imgcount { font-size: 0.75em; color: var(--muted); }
    .entry.hidden-entry { opacity: 0.5; }
    .entry.hidden-entry .date { text-decoration: line-through; }
    .entry .eye { border: none; background: none; cursor: pointer; font-size: 1em; padding: 0 0.2em; opacity: 0.7; }
    .entry .eye:hover { opacity: 1; }
    .meta { font-size: 0.8em; color: var(--muted); margin-bottom: 0.5em; }
    .news-item { display: flex; gap: 0.5em; align-items: flex-start; margin-bottom: 0.4em; }
    .news-item textarea { min-height: 2.6em; font-family: inherit; }
    .news-item button { flex-shrink: 0; }

    /* Bulk bar */
    #bulk-bar { position: sticky; top: 112px; z-index: 18; background: #2b2b2b; color: white; border-radius: 6px; padding: 0.7em 1em; margin-bottom: 1em; display: flex; gap: 0.6em; align-items: center; flex-wrap: wrap; }
    #bulk-bar[hidden] { display: none; }
    #bulk-bar select { width: auto; background: white; }
    #bulk-bar .bulk-values { display: flex; gap: 0.4em; flex-wrap: wrap; }
    #bulk-bar .bulk-values label { display: inline-flex; align-items: center; gap: 0.25em; margin: 0; color: white; font-size: 0.85em; padding: 0.15em 0.45em; border: 1px solid #666; border-radius: 4px; cursor: pointer; }
    #bulk-bar .bulk-values label.checked { background: var(--accent); border-color: var(--accent); }
    #bulk-bar .bulk-values input { display: none; }
    #bulk-bar .spacer { flex: 1; }

    /* Modal */
    .modal-bg { position: fixed; inset: 0; background: rgba(0,0,0,0.4); display: none; align-items: flex-start; justify-content: center; padding: 3em 1em; overflow-y: auto; z-index: 50; }
    .modal-bg.open { display: flex; }
    .modal { background: white; border-radius: 8px; max-width: 720px; width: 100%; padding: 1.5em; box-shadow: 0 4px 32px rgba(0,0,0,0.2); }
    .modal h2 { margin-top: 0; }
    .row { display: grid; grid-template-columns: 1fr 1fr; gap: 1em; }
    .checkbox-group { display: flex; flex-wrap: wrap; gap: 0.5em; padding: 0.3em 0; }
    .checkbox-group label { display: inline-flex; align-items: center; gap: 0.3em; margin: 0; font-size: 0.9em; color: var(--fg); cursor: pointer; padding: 0.2em 0.5em; border: 1px solid var(--border); border-radius: 4px; }
    .checkbox-group label.checked { background: var(--accent); color: white; border-color: var(--accent); }
    .checkbox-group input { display: none; }
    .modal .actions { margin-top: 1.5em; display: flex; justify-content: space-between; gap: 0.5em; align-items: center; }
    .modal .actions .right { display: flex; gap: 0.5em; }

    /* Images in modal */
    #dropzone, #gallery-dropzone { border: 2px dashed var(--border); border-radius: 6px; padding: 1em; text-align: center; color: var(--muted); cursor: pointer; margin-top: 0.3em; transition: background 0.15s, border-color 0.15s; }
    #dropzone.dragover, #gallery-dropzone.dragover { background: #fff4ef; border-color: var(--accent); color: var(--accent); }
    #gallery-list { margin-top: 0.7em; }
    .gal-item { display: flex; gap: 0.7em; align-items: flex-start; border: 1px solid var(--border); border-radius: 6px; padding: 0.5em; margin-bottom: 0.5em; background: var(--bg); }
    .gal-item img { width: 120px; height: 80px; object-fit: cover; border-radius: 4px; flex-shrink: 0; background: #eee; }
    .gal-item .fields { flex: 1; display: flex; flex-direction: column; gap: 0.1em; min-width: 0; }
    .gal-item .fields textarea { min-height: 2.6em; }
    .gal-item .ctrls { display: flex; flex-direction: column; gap: 0.2em; }
    .fld { display: block; font-size: 0.72em; color: var(--muted); margin: 0.3em 0 0.1em; }
    .fld:first-child { margin-top: 0; }
    .img-item .fld { margin: 0.2em 0 0; }
    #images-list { display: flex; flex-wrap: wrap; gap: 0.6em; margin-top: 0.6em; }
    .img-item { width: 130px; border: 1px solid var(--border); border-radius: 6px; padding: 0.4em; background: var(--bg); position: relative; }
    .img-item img { width: 100%; height: 80px; object-fit: cover; border-radius: 4px; display: block; background: #eee; }
    .img-item input { font-size: 0.8em; margin-top: 0.3em; padding: 0.2em 0.4em; }
    .img-item .rm { position: absolute; top: 4px; right: 4px; background: var(--accent); color: white; border: none; border-radius: 50%; width: 20px; height: 20px; cursor: pointer; line-height: 18px; padding: 0; text-align: center; box-shadow: 0 1px 4px rgba(0,0,0,0.3); }

    .theme-panel { background: white; border: 1px solid var(--border); border-radius: 6px; padding: 1em; margin-bottom: 1em; }
    .theme-panel h3 { margin: 0 0 0.5em 0; font-size: 0.95em; }
    .theme-row { display: flex; gap: 0.5em; align-items: center; margin-bottom: 0.3em; }
    #build-output { position: fixed; bottom: 1em; left: 1em; width: 420px; max-width: 42vw; z-index: 70; font-family: ui-monospace, monospace; background: #111; color: #cfc; padding: 1em 1em 0.8em; border-radius: 6px; white-space: pre-wrap; max-height: 38vh; overflow-y: auto; font-size: 0.8em; display: none; box-shadow: 0 4px 24px rgba(0,0,0,0.35); cursor: pointer; }
    #build-output.show { display: block; }
    #build-output.error { color: #fcc; }
    #build-output::after { content: "click to dismiss"; display: block; margin-top: 0.6em; opacity: 0.5; font-size: 0.9em; }
    .empty { text-align: center; padding: 2em; color: var(--muted); }
  </style>
</head>
<body>
  <header>
    <h1>zhexi.info CMS</h1>
    <span class="meta" id="count"></span>
    <span class="spacer"></span>
    <button id="add-btn">+ new entry</button>
    <button id="build-btn" class="primary">build site</button>
  </header>

  <div class="toolbar">
    <input type="text" id="search" placeholder="search…">
    <label style="margin:0;display:flex;align-items:center;gap:0.3em;">sort
      <select id="sort-by" style="width:auto">
        <option value="sort">date (timeline)</option>
        <option value="type">type</option>
        <option value="created">date added</option>
        <option value="modified">date modified</option>
        <option value="id">id</option>
      </select>
    </label>
    <label style="margin:0;display:flex;align-items:center;gap:0.3em;">show
      <select id="vis-filter" style="width:auto">
        <option value="all">all</option>
        <option value="visible">visible only</option>
        <option value="hidden">hidden only</option>
      </select>
    </label>
    <label style="margin:0;display:flex;align-items:center;gap:0.4em;"><input type="checkbox" id="filter-images" style="width:auto"> with images</label>
    <button id="select-all" class="tiny">select all</button>
    <span style="flex:1"></span>
    <label style="margin:0;display:flex;align-items:center;gap:0.4em;"><input type="checkbox" id="show-themes" style="width:auto"> themes</label>
    <label style="margin:0;display:flex;align-items:center;gap:0.4em;"><input type="checkbox" id="show-site" style="width:auto"> site content</label>
    <label style="margin:0;display:flex;align-items:center;gap:0.4em;"><input type="checkbox" id="show-gallery" style="width:auto"> gallery</label>
  </div>

  <main>
    <div id="theme-panel" class="theme-panel" style="display:none;">
      <h3>research themes</h3>
      <div id="themes-list"></div>
      <div class="theme-row" style="margin-top:0.8em;">
        <input type="text" id="new-theme-slug" placeholder="slug (e.g. sinofuturism)">
        <input type="text" id="new-theme-label" placeholder="label (e.g. Sinofuturism)">
        <button id="add-theme-btn" class="tiny">add theme</button>
      </div>
    </div>

    <div id="site-panel" class="theme-panel" style="display:none;">
      <h3>site content <small style="font-weight:400;color:var(--muted)">— untick a module to hide it from the site</small></h3>

      <label class="mod"><input type="checkbox" id="show-heading" style="width:auto"> main heading <small>(HTML)</small></label>
      <input type="text" id="site-heading">

      <label class="mod" style="margin-top:0.9em;"><input type="checkbox" id="show-news" style="width:auto"> news <small>(HTML per item)</small></label>
      <div id="news-items"></div>
      <button id="add-news" class="tiny" style="margin-top:0.4em;">+ add news item</button>

      <label class="mod" style="margin-top:0.9em;"><input type="checkbox" id="show-bio" style="width:auto"> bio <small>(HTML; &lt;br&gt;&lt;br&gt; between paragraphs)</small></label>
      <textarea id="site-bio" rows="10"></textarea>

      <label class="mod" style="margin-top:0.9em;"><input type="checkbox" id="show-affiliations" style="width:auto"> affiliations <small>(HTML per item)</small></label>
      <div id="affiliations-items"></div>
      <button id="add-affiliation" class="tiny" style="margin-top:0.4em;">+ add affiliation</button>

      <label class="mod" style="margin-top:0.9em;"><input type="checkbox" id="show-links" style="width:auto"> links <small>(HTML per item)</small></label>
      <div id="links-items"></div>
      <button id="add-link" class="tiny" style="margin-top:0.4em;">+ add link</button>

      <div style="margin-top:1em;"><button id="save-site" class="primary">save site content</button> <span class="meta" id="site-saved"></span></div>
    </div>

    <div id="gallery-panel" class="theme-panel" style="display:none;">
      <h3>gallery <small style="font-weight:400;color:var(--muted)">— independent artworks for the image carousel (shown before captioned entry images)</small></h3>
      <div id="gallery-dropzone">drop artwork images here, or click to choose</div>
      <input type="file" id="gallery-file-input" accept="image/*" multiple hidden>
      <div id="gallery-list"></div>
      <label class="mod" style="margin-top:0.8em;"><input type="checkbox" id="gallery-shuffle" style="width:auto"> randomise order in the carousel <small>(otherwise shown in the order above)</small></label>
      <div style="margin-top:0.8em;"><button id="save-gallery" class="primary">save gallery</button> <span class="meta" id="gallery-saved"></span></div>
    </div>

    <div id="bulk-bar" hidden>
      <strong id="bulk-count"></strong>
      <select id="bulk-field">
        <option value="types">types</option>
        <option value="themes">themes</option>
      </select>
      <select id="bulk-action">
        <option value="add">add</option>
        <option value="remove">remove</option>
        <option value="set">set</option>
      </select>
      <span class="bulk-values" id="bulk-values"></span>
      <button id="bulk-apply" class="primary tiny">apply</button>
      <span style="width:1px;height:1.4em;background:#555;"></span>
      <button id="bulk-show" class="tiny">show</button>
      <button id="bulk-hide" class="tiny">hide</button>
      <span class="spacer"></span>
      <button id="bulk-delete" class="danger tiny">delete selected</button>
      <button id="bulk-clear" class="tiny">clear</button>
    </div>

    <div id="entries"></div>
    <div id="build-output"></div>
  </main>

  <div class="modal-bg" id="modal">
    <div class="modal">
      <h2 id="modal-title">edit entry</h2>
      <input type="hidden" id="entry-id">

      <div class="row">
        <div>
          <label>display date <small>(e.g. "sept 2025", "2024", "apr-jul 2025")</small></label>
          <input type="text" id="f-date">
        </div>
        <div>
          <label>sort date <small>(YYYY-MM-DD; used for ordering)</small></label>
          <input type="date" id="f-sort">
        </div>
      </div>

      <label style="display:flex;align-items:center;gap:0.4em;margin-top:0.7em;"><input type="checkbox" id="f-visible" style="width:auto"> visible on site</label>

      <label>types <small>(first is the visible label; click order sets priority)</small></label>
      <div class="checkbox-group" id="f-types"></div>

      <label>themes</label>
      <div class="checkbox-group" id="f-themes"></div>
      <div class="theme-row" style="margin-top:0.4em;">
        <input type="text" id="modal-new-theme" placeholder="+ new theme name" style="font-size:0.85em; max-width:18em;">
        <button type="button" id="modal-add-theme" class="tiny">add theme</button>
      </div>

      <label>description (HTML allowed: &lt;a&gt;, &lt;em&gt;, &amp;mdash;, &amp;amp;)</label>
      <div style="margin-bottom:0.3em; display:flex; gap:0.5em; align-items:center;">
        <button type="button" id="desc-link-btn" class="tiny">🔗 insert link</button>
        <button type="button" id="desc-em-btn" class="tiny"><em>i</em> italic</button>
        <span class="meta" style="margin:0;">select text first, then click</span>
      </div>
      <textarea id="f-desc" rows="5"></textarea>

      <label>images <small>(drag files in, or click to browse — saved to images/)</small></label>
      <div id="dropzone">drop images here, or click to choose files</div>
      <input type="file" id="file-input" accept="image/*" multiple hidden>
      <div id="images-list"></div>

      <div class="actions">
        <button id="delete-btn" class="danger" style="display:none;">delete</button>
        <span></span>
        <div class="right">
          <button id="cancel-btn">cancel</button>
          <button id="save-btn" class="primary">save</button>
        </div>
      </div>
    </div>
  </div>

<script>
  const ALLOWED_TYPES = ['exhibition', 'writing', 'research', 'talk', 'teaching'];
  let data = { site: {}, themes: [], entries: [] };
  let currentImages = [];          // working images for the open modal
  let siteNews = [], siteAffiliations = [], siteLinks = [];   // working site-content lists
  let siteGallery = [];                                        // working carousel artworks
  const selected = new Set();      // selected entry ids for bulk ops

  function esc(s) {
    return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  }

  // ── Loading ──────────────────────────────────────────────────────────────
  async function load() {
    const res = await fetch('/api/data');
    data = await res.json();
    render();
    renderThemes();
    renderBulkBar();
    renderSiteEditor();
    renderGalleryEditor();
  }

  // ── Gallery (carousel artworks) ──────────────────────────────────────────
  function renderGalleryEditor() {
    siteGallery = (data.gallery || []).map(g => ({ src: g.src, caption: g.caption || '', alt: g.alt || '' }));
    document.getElementById('gallery-shuffle').checked = !!data.gallery_shuffle;
    renderGallery();
  }

  function renderGallery() {
    const wrap = document.getElementById('gallery-list');
    if (!siteGallery.length) { wrap.innerHTML = '<div class="meta">no artworks yet — drop images above.</div>'; return; }
    wrap.innerHTML = siteGallery.map((g, i) => `
      <div class="gal-item" data-i="${i}">
        <img src="/${esc(g.src)}" alt="">
        <div class="fields">
          <label class="fld">caption — shown on the carousel; HTML ok (&lt;em&gt; = italic title)</label>
          <textarea data-cap="${i}" rows="2" placeholder="e.g. &lt;em&gt;Title&lt;/em&gt;, 2025. Medium.">${esc(g.caption || '')}</textarea>
          <label class="fld">alt text — accessibility only, plain text, not shown</label>
          <input type="text" data-alt="${i}" placeholder="describe the image" value="${esc(g.alt || '')}">
        </div>
        <div class="ctrls">
          <button class="tiny" data-up="${i}" title="move up">↑</button>
          <button class="tiny" data-down="${i}" title="move down">↓</button>
          <button class="tiny danger" data-rm="${i}" title="remove">×</button>
        </div>
      </div>`).join('');
    wrap.querySelectorAll('textarea[data-cap]').forEach(t => t.addEventListener('input', () => { siteGallery[+t.dataset.cap].caption = t.value; }));
    wrap.querySelectorAll('input[data-alt]').forEach(t => t.addEventListener('input', () => { siteGallery[+t.dataset.alt].alt = t.value; }));
    wrap.querySelectorAll('button[data-rm]').forEach(b => b.addEventListener('click', () => { siteGallery.splice(+b.dataset.rm, 1); renderGallery(); }));
    wrap.querySelectorAll('button[data-up]').forEach(b => b.addEventListener('click', () => { const i = +b.dataset.up; if (i > 0) { [siteGallery[i - 1], siteGallery[i]] = [siteGallery[i], siteGallery[i - 1]]; renderGallery(); } }));
    wrap.querySelectorAll('button[data-down]').forEach(b => b.addEventListener('click', () => { const i = +b.dataset.down; if (i < siteGallery.length - 1) { [siteGallery[i + 1], siteGallery[i]] = [siteGallery[i], siteGallery[i + 1]]; renderGallery(); } }));
  }

  async function uploadGallery(fileList) {
    if (!fileList || !fileList.length) return;
    const fd = new FormData();
    for (const f of fileList) fd.append('files', f);
    const dz = document.getElementById('gallery-dropzone');
    const prev = dz.textContent; dz.textContent = 'uploading…';
    const res = await fetch('/api/upload', { method: 'POST', body: fd });
    const r = await res.json(); dz.textContent = prev;
    (r.paths || []).forEach(p => siteGallery.push({ src: p, caption: '', alt: '' }));
    if (r.skipped && r.skipped.length) alert('skipped (unsupported type): ' + r.skipped.join(', '));
    renderGallery();
  }

  async function saveGallery() {
    const shuffle = document.getElementById('gallery-shuffle').checked;
    const res = await fetch('/api/gallery', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ gallery: siteGallery, shuffle }) });
    const note = document.getElementById('gallery-saved');
    if (res.ok) { note.textContent = 'saved ✓ — click "build site" to publish'; await load(); }
    else { note.textContent = 'error saving'; }
  }

  function filteredEntries() {
    const search = document.getElementById('search').value.toLowerCase().trim();
    const sortBy = document.getElementById('sort-by').value;
    const vis = document.getElementById('vis-filter').value;

    const imagesOnly = document.getElementById('filter-images').checked;
    let list = data.entries.filter(e =>
      vis === 'all' ||
      (vis === 'visible' && e.visible !== false) ||
      (vis === 'hidden' && e.visible === false)
    );
    if (imagesOnly) list = list.filter(e => (e.images || []).length);
    list = list.filter(e => !search ||
      (e.description_html || '').toLowerCase().includes(search) ||
      (e.date || '').toLowerCase().includes(search) ||
      (e.types || []).join(' ').toLowerCase().includes(search) ||
      (e.themes || []).join(' ').toLowerCase().includes(search)
    );

    const cmp = {
      sort: (a, b) => (b.sort || '').localeCompare(a.sort || ''),
      type: (a, b) => ((a.types || [])[0] || '').localeCompare((b.types || [])[0] || '') || (b.sort || '').localeCompare(a.sort || ''),
      created: (a, b) => (b.created || '').localeCompare(a.created || ''),
      modified: (a, b) => (b.modified || '').localeCompare(a.modified || ''),
      id: (a, b) => (b.id || 0) - (a.id || 0),
    }[sortBy] || ((a, b) => 0);
    return [...list].sort(cmp);
  }

  // ── Render entries list ──────────────────────────────────────────────────
  function render() {
    const filtered = filteredEntries();
    const container = document.getElementById('entries');
    document.getElementById('count').textContent = `${filtered.length} of ${data.entries.length} entries`;

    if (!filtered.length) {
      container.innerHTML = '<div class="empty">no entries match</div>';
      return;
    }

    container.innerHTML = filtered.map(e => {
      const nImg = (e.images || []).length;
      const hidden = e.visible === false;
      return `
      <div class="entry ${selected.has(e.id) ? 'selected' : ''} ${hidden ? 'hidden-entry' : ''}" data-id="${e.id}">
        <input type="checkbox" class="sel" ${selected.has(e.id) ? 'checked' : ''}>
        <div class="date body">${esc(e.date || '')}</div>
        <div class="types body">${(e.types || []).map(t => `<span class="pill type type-${esc(t)}">${esc(t)}</span>`).join('')}</div>
        <div class="desc body">${(e.description_html || '').replace(/<[^>]+>/g, '')}</div>
        <div class="meta-right">
          ${nImg ? `<span class="imgcount">🖼 ${nImg}</span>` : ''}
          ${(e.themes || []).map(t => `<span class="pill theme">${esc(t)}</span>`).join('')}
          <button class="eye" title="${hidden ? 'hidden — click to show' : 'visible — click to hide'}">${hidden ? '🙈' : '👁'}</button>
        </div>
      </div>`;
    }).join('');

    container.querySelectorAll('.entry').forEach(el => {
      const id = parseInt(el.dataset.id, 10);
      el.querySelector('.sel').addEventListener('change', ev => {
        if (ev.target.checked) selected.add(id); else selected.delete(id);
        el.classList.toggle('selected', ev.target.checked);
        renderBulkBar();
      });
      el.querySelector('.eye').addEventListener('click', ev => {
        ev.stopPropagation();
        const ent = data.entries.find(x => x.id === id);
        toggleVisible(id, ent.visible === false);
      });
      el.querySelectorAll('.body').forEach(b => b.addEventListener('click', () => openModal(id)));
    });
  }

  async function toggleVisible(id, makeVisible) {
    const res = await fetch(`/api/entries/${id}/visibility`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ visible: makeVisible }),
    });
    if (res.ok) await load();
  }

  // ── Bulk bar ───────────────────────────────────────────────────────────────
  function renderBulkBar() {
    const bar = document.getElementById('bulk-bar');
    if (!selected.size) { bar.hidden = true; return; }
    bar.hidden = false;
    document.getElementById('bulk-count').textContent = `${selected.size} selected`;
    renderBulkValues();
  }

  function renderBulkValues() {
    const field = document.getElementById('bulk-field').value;
    const wrap = document.getElementById('bulk-values');
    const opts = field === 'types'
      ? ALLOWED_TYPES.map(t => ({ v: t, l: t }))
      : data.themes.map(t => ({ v: t.slug, l: t.label || t.slug }));
    if (!opts.length) { wrap.innerHTML = '<span style="opacity:0.6">no themes defined</span>'; return; }
    wrap.innerHTML = opts.map(o =>
      `<label data-v="${esc(o.v)}"><input type="checkbox" value="${esc(o.v)}">${esc(o.l)}</label>`
    ).join('');
    wrap.querySelectorAll('label').forEach(label => {
      label.addEventListener('click', e => {
        e.preventDefault();
        const cb = label.querySelector('input');
        cb.checked = !cb.checked;
        label.classList.toggle('checked', cb.checked);
      });
    });
  }

  async function bulkApply() {
    const field = document.getElementById('bulk-field').value;
    const action = document.getElementById('bulk-action').value;
    const values = [...document.querySelectorAll('#bulk-values input:checked')].map(c => c.value);
    if (!values.length) { alert('pick at least one value'); return; }
    const res = await fetch('/api/bulk', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ids: [...selected], operations: [{ field, action, values }] }),
    });
    const r = await res.json();
    if (!res.ok) { alert('error: ' + r.error); return; }
    selected.clear();
    await load();
  }

  async function bulkDelete() {
    if (!confirm(`delete ${selected.size} selected entries? this cannot be undone.`)) return;
    const res = await fetch('/api/bulk', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ids: [...selected], delete: true }),
    });
    if (res.ok) { selected.clear(); await load(); }
  }

  async function bulkSetVisible(val) {
    const res = await fetch('/api/bulk', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ids: [...selected], set_visible: val }),
    });
    if (res.ok) await load();
  }

  // ── Site content editor ─────────────────────────────────────────────────────
  const SITE_MODULES = ['heading', 'news', 'bio', 'affiliations', 'links'];

  function renderSiteEditor() {
    const s = data.site || {};
    document.getElementById('site-heading').value = s.heading_html || '';
    document.getElementById('site-bio').value = s.bio_html || '';
    siteNews = [...(s.news || [])];
    siteAffiliations = [...(s.affiliations || [])];
    siteLinks = [...(s.links || [])];
    renderList('news-items', siteNews);
    renderList('affiliations-items', siteAffiliations);
    renderList('links-items', siteLinks);
    const show = s.show || {};
    SITE_MODULES.forEach(k => {
      const cb = document.getElementById('show-' + k);
      if (cb) cb.checked = show[k] !== false;
    });
  }

  // Generic editable list of HTML strings (used for news / affiliations / links).
  function renderList(containerId, arr) {
    const wrap = document.getElementById(containerId);
    wrap.innerHTML = arr.map((n, i) => `
      <div class="news-item">
        <textarea data-i="${i}" rows="2">${esc(n)}</textarea>
        <button class="tiny danger" data-rm="${i}">×</button>
      </div>`).join('');
    wrap.querySelectorAll('textarea[data-i]').forEach(t => {
      t.addEventListener('input', () => { arr[+t.dataset.i] = t.value; });
    });
    wrap.querySelectorAll('button[data-rm]').forEach(b => {
      b.addEventListener('click', () => { arr.splice(+b.dataset.rm, 1); renderList(containerId, arr); });
    });
  }

  async function saveSite() {
    const show = {};
    SITE_MODULES.forEach(k => {
      const cb = document.getElementById('show-' + k);
      show[k] = cb ? cb.checked : true;
    });
    const payload = {
      heading_html: document.getElementById('site-heading').value,
      bio_html: document.getElementById('site-bio').value,
      news: siteNews,
      affiliations: siteAffiliations,
      links: siteLinks,
      show,
    };
    const res = await fetch('/api/site', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
    const note = document.getElementById('site-saved');
    if (res.ok) { note.textContent = 'saved ✓ — click "build site" to publish'; await load(); }
    else { note.textContent = 'error saving'; }
  }

  // ── Theme panel ────────────────────────────────────────────────────────────
  function renderThemes() {
    const list = document.getElementById('themes-list');
    if (!data.themes.length) { list.innerHTML = '<div class="meta">no themes defined yet.</div>'; return; }
    list.innerHTML = data.themes.map(t => `
      <div class="theme-row" data-orig="${esc(t.slug)}">
        <input type="text" class="th-slug" value="${esc(t.slug)}" style="max-width:11em">
        <input type="text" class="th-label" value="${esc(t.label || t.slug)}">
        <button class="tiny th-save">save</button>
        <button class="tiny danger th-del">remove</button>
      </div>`).join('');
    list.querySelectorAll('.theme-row').forEach(row => {
      const orig = row.dataset.orig;
      row.querySelector('.th-save').addEventListener('click', async () => {
        const slug = row.querySelector('.th-slug').value.trim().toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '');
        const label = row.querySelector('.th-label').value.trim() || slug;
        if (!slug) return;
        const res = await fetch(`/api/themes/${encodeURIComponent(orig)}`, {
          method: 'PUT', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ slug, label }),
        });
        const r = await res.json();
        if (!res.ok) { alert('error: ' + r.error); return; }
        await load();
      });
      row.querySelector('.th-del').addEventListener('click', async () => {
        if (!confirm(`delete theme "${orig}"? it will be removed from all entries that use it.`)) return;
        const res = await fetch(`/api/themes/${encodeURIComponent(orig)}`, { method: 'DELETE' });
        if (res.ok) await load();
      });
    });
  }

  // ── Modal ────────────────────────────────────────────────────────────────
  function openModal(id) {
    const entry = id ? data.entries.find(e => e.id === id) : null;
    document.getElementById('modal-title').textContent = entry ? `edit entry #${id}` : 'new entry';
    document.getElementById('entry-id').value = entry ? entry.id : '';
    document.getElementById('f-date').value = entry?.date || '';
    document.getElementById('f-sort').value = entry?.sort || '';
    document.getElementById('f-desc').value = entry?.description_html || '';
    document.getElementById('f-visible').checked = entry ? entry.visible !== false : true;
    document.getElementById('delete-btn').style.display = entry ? '' : 'none';

    currentImages = (entry?.images || []).map(im => ({ src: im.src, alt: im.alt || '', caption: im.caption || '' }));
    renderModalImages();

    // Types (ordered: click order sets priority, first = visible label)
    const typesContainer = document.getElementById('f-types');
    typesContainer._order = [...(entry?.types || [])];
    drawTypes();
    function drawTypes() {
      const order = typesContainer._order;
      typesContainer.innerHTML = ALLOWED_TYPES.map(t => {
        const i = order.indexOf(t);
        return `<label class="${i !== -1 ? 'checked' : ''}" data-type="${t}">
          <input type="checkbox" ${i !== -1 ? 'checked' : ''}>${t}${i !== -1 ? ` (${i + 1})` : ''}
        </label>`;
      }).join('');
      typesContainer.querySelectorAll('label').forEach(label => {
        label.addEventListener('click', e => {
          e.preventDefault();
          const t = label.dataset.type;
          const idx = order.indexOf(t);
          if (idx === -1) order.push(t); else order.splice(idx, 1);
          drawTypes();
        });
      });
    }

    // Themes
    drawModalThemes(entry?.themes || []);

    document.getElementById('modal').classList.add('open');
  }

  function drawModalThemes(selectedSlugs) {
    const c = document.getElementById('f-themes');
    if (!data.themes.length) {
      c.innerHTML = '<span class="meta" style="margin:0;">no themes yet — add one below.</span>';
      return;
    }
    c.innerHTML = data.themes.map(th =>
      `<label class="${selectedSlugs.includes(th.slug) ? 'checked' : ''}" data-theme="${esc(th.slug)}">
        <input type="checkbox" ${selectedSlugs.includes(th.slug) ? 'checked' : ''} value="${esc(th.slug)}">${esc(th.label || th.slug)}
      </label>`).join('');
    c.querySelectorAll('label').forEach(label => {
      label.addEventListener('click', e => {
        e.preventDefault();
        const cb = label.querySelector('input');
        cb.checked = !cb.checked;
        label.classList.toggle('checked', cb.checked);
      });
    });
  }

  function modalSelectedThemes() {
    return [...document.querySelectorAll('#f-themes input:checked')].map(c => c.value);
  }

  function renderModalImages() {
    const wrap = document.getElementById('images-list');
    if (!currentImages.length) { wrap.innerHTML = ''; return; }
    wrap.innerHTML = currentImages.map((im, i) => `
      <div class="img-item" data-i="${i}">
        <button class="rm" data-rm="${i}" title="remove">×</button>
        <img src="/${esc(im.src)}" alt="">
        <label class="fld">caption (HTML)</label>
        <input type="text" placeholder="&lt;em&gt;Title&lt;/em&gt;, year. Medium." value="${esc(im.caption || '')}" data-caption="${i}">
        <label class="fld">alt (plain)</label>
        <input type="text" placeholder="describe the image" value="${esc(im.alt || '')}" data-alt="${i}">
      </div>`).join('');
    wrap.querySelectorAll('input[data-caption]').forEach(inp => {
      inp.addEventListener('input', () => { currentImages[+inp.dataset.caption].caption = inp.value; });
    });
    wrap.querySelectorAll('input[data-alt]').forEach(inp => {
      inp.addEventListener('input', () => { currentImages[+inp.dataset.alt].alt = inp.value; });
    });
    wrap.querySelectorAll('button[data-rm]').forEach(btn => {
      btn.addEventListener('click', () => { currentImages.splice(+btn.dataset.rm, 1); renderModalImages(); });
    });
  }

  async function uploadFiles(fileList) {
    if (!fileList || !fileList.length) return;
    const fd = new FormData();
    for (const f of fileList) fd.append('files', f);
    const dz = document.getElementById('dropzone');
    const prev = dz.textContent;
    dz.textContent = 'uploading…';
    const res = await fetch('/api/upload', { method: 'POST', body: fd });
    const r = await res.json();
    dz.textContent = prev;
    (r.paths || []).forEach(p => currentImages.push({ src: p, alt: '', caption: '' }));
    if (r.skipped && r.skipped.length) alert('skipped (unsupported type): ' + r.skipped.join(', '));
    renderModalImages();
  }

  function closeModal() { document.getElementById('modal').classList.remove('open'); }

  async function save() {
    const id = document.getElementById('entry-id').value;
    const types = document.getElementById('f-types')._order || [];
    const themes = [...document.getElementById('f-themes').querySelectorAll('input:checked')].map(c => c.value);
    const payload = {
      date: document.getElementById('f-date').value.trim(),
      sort: document.getElementById('f-sort').value,
      visible: document.getElementById('f-visible').checked,
      types, themes,
      description_html: document.getElementById('f-desc').value.trim(),
      images: currentImages,
    };
    const url = id ? `/api/entries/${id}` : '/api/entries';
    const method = id ? 'PUT' : 'POST';
    const res = await fetch(url, { method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
    if (!res.ok) { const err = await res.json(); alert('error: ' + err.error); return; }
    closeModal();
    await load();
  }

  async function deleteEntry() {
    const id = parseInt(document.getElementById('entry-id').value, 10);
    if (!id || !confirm(`delete entry #${id}?`)) return;
    const res = await fetch(`/api/entries/${id}`, { method: 'DELETE' });
    if (res.ok) { closeModal(); await load(); }
  }

  async function addTheme() {
    const slug = document.getElementById('new-theme-slug').value.trim().toLowerCase().replace(/[^a-z0-9-]/g, '-');
    const label = document.getElementById('new-theme-label').value.trim() || slug;
    if (!slug) return;
    const res = await fetch('/api/themes', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ slug, label }) });
    if (res.ok) {
      document.getElementById('new-theme-slug').value = '';
      document.getElementById('new-theme-label').value = '';
      await load();
    } else { alert('error: ' + (await res.json()).error); }
  }

  async function build() {
    const out = document.getElementById('build-output');
    out.className = 'show';
    out.textContent = 'building…';
    const res = await fetch('/api/build', { method: 'POST' });
    const r = await res.json();
    out.className = 'show' + (r.ok ? '' : ' error');
    out.textContent = (r.stdout || '') + (r.stderr ? '\n' + r.stderr : '') + (r.ok ? '\n✓ build complete' : `\n✗ build failed (exit ${r.returncode})`);
  }

  // ── Wiring ───────────────────────────────────────────────────────────────
  document.getElementById('add-btn').addEventListener('click', () => openModal(null));
  document.getElementById('cancel-btn').addEventListener('click', closeModal);
  document.getElementById('save-btn').addEventListener('click', save);
  document.getElementById('delete-btn').addEventListener('click', deleteEntry);
  document.getElementById('build-btn').addEventListener('click', build);
  document.getElementById('build-output').addEventListener('click', e => { e.currentTarget.className = ''; });
  document.getElementById('add-theme-btn').addEventListener('click', addTheme);
  document.getElementById('search').addEventListener('input', render);
  document.getElementById('sort-by').addEventListener('change', render);
  document.getElementById('vis-filter').addEventListener('change', render);
  document.getElementById('filter-images').addEventListener('change', render);
  document.getElementById('show-themes').addEventListener('change', e => {
    document.getElementById('theme-panel').style.display = e.target.checked ? '' : 'none';
  });
  document.getElementById('show-site').addEventListener('change', e => {
    document.getElementById('site-panel').style.display = e.target.checked ? '' : 'none';
  });
  document.getElementById('show-gallery').addEventListener('change', e => {
    document.getElementById('gallery-panel').style.display = e.target.checked ? '' : 'none';
  });
  document.getElementById('save-gallery').addEventListener('click', saveGallery);
  (function () {
    const gdz = document.getElementById('gallery-dropzone');
    const gfi = document.getElementById('gallery-file-input');
    gdz.addEventListener('click', () => gfi.click());
    gfi.addEventListener('change', () => { uploadGallery(gfi.files); gfi.value = ''; });
    ['dragenter', 'dragover'].forEach(ev => gdz.addEventListener(ev, e => { e.preventDefault(); gdz.classList.add('dragover'); }));
    ['dragleave', 'drop'].forEach(ev => gdz.addEventListener(ev, e => { e.preventDefault(); gdz.classList.remove('dragover'); }));
    gdz.addEventListener('drop', e => uploadGallery(e.dataTransfer.files));
  })();
  document.getElementById('add-news').addEventListener('click', () => { siteNews.push(''); renderList('news-items', siteNews); });
  document.getElementById('add-affiliation').addEventListener('click', () => { siteAffiliations.push(''); renderList('affiliations-items', siteAffiliations); });
  document.getElementById('add-link').addEventListener('click', () => { siteLinks.push(''); renderList('links-items', siteLinks); });
  document.getElementById('save-site').addEventListener('click', saveSite);
  document.getElementById('bulk-show').addEventListener('click', () => bulkSetVisible(true));
  document.getElementById('bulk-hide').addEventListener('click', () => bulkSetVisible(false));
  document.getElementById('select-all').addEventListener('click', () => {
    const filtered = filteredEntries();
    const allSelected = filtered.length && filtered.every(en => selected.has(en.id));
    if (allSelected) filtered.forEach(en => selected.delete(en.id));
    else filtered.forEach(en => selected.add(en.id));
    render(); renderBulkBar();
  });
  document.getElementById('bulk-field').addEventListener('change', renderBulkValues);
  document.getElementById('bulk-apply').addEventListener('click', bulkApply);
  document.getElementById('bulk-delete').addEventListener('click', bulkDelete);
  document.getElementById('bulk-clear').addEventListener('click', () => { selected.clear(); render(); renderBulkBar(); });
  document.getElementById('modal').addEventListener('click', e => { if (e.target.id === 'modal') closeModal(); });

  // Add a theme from inside the entry modal (adds globally + selects it here)
  document.getElementById('modal-add-theme').addEventListener('click', async () => {
    const inp = document.getElementById('modal-new-theme');
    const raw = inp.value.trim();
    if (!raw) return;
    const slug = raw.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '');
    const res = await fetch('/api/themes', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ slug, label: raw }) });
    const r = await res.json();
    if (!res.ok) { alert('error: ' + r.error); return; }
    data.themes.push({ slug: r.slug, label: r.label });
    const cur = modalSelectedThemes();
    cur.push(r.slug);
    inp.value = '';
    drawModalThemes(cur);
    renderThemes();
  });

  // Description helpers: wrap the textarea selection in a tag.
  function wrapSelection(before, after) {
    const ta = document.getElementById('f-desc');
    const s = ta.selectionStart, e = ta.selectionEnd;
    const sel = ta.value.slice(s, e);
    ta.value = ta.value.slice(0, s) + before + sel + after + ta.value.slice(e);
    ta.focus();
    ta.selectionStart = s + before.length;
    ta.selectionEnd = s + before.length + sel.length;
  }
  document.getElementById('desc-link-btn').addEventListener('click', () => {
    const ta = document.getElementById('f-desc');
    const sel = ta.value.slice(ta.selectionStart, ta.selectionEnd);
    const url = prompt('link URL:', 'https://');
    if (!url) return;
    const text = sel || prompt('link text:', '') || url;
    const s = ta.selectionStart, e = ta.selectionEnd;
    const link = `<a href="${url}">${text}</a>`;
    ta.value = ta.value.slice(0, s) + link + ta.value.slice(e);
    ta.focus();
  });
  document.getElementById('desc-em-btn').addEventListener('click', () => wrapSelection('<em>', '</em>'));

  // Display date drives the sort date (override the sort field afterward if needed).
  const MA = {'1':'jan','01':'jan','jan':'jan','january':'jan','2':'feb','02':'feb','feb':'feb','february':'feb','3':'mar','03':'mar','mar':'mar','march':'mar','4':'apr','04':'apr','apr':'apr','april':'apr','5':'may','05':'may','may':'may','6':'jun','06':'jun','jun':'jun','june':'jun','7':'jul','07':'jul','jul':'jul','july':'jul','8':'aug','08':'aug','aug':'aug','august':'aug','9':'sept','09':'sept','sep':'sept','sept':'sept','september':'sept','10':'oct','oct':'oct','october':'oct','11':'nov','nov':'nov','november':'nov','12':'dec','dec':'dec','december':'dec'};
  const MN = {jan:1,feb:2,mar:3,apr:4,may:5,jun:6,jul:7,aug:8,sept:9,oct:10,nov:11,dec:12};
  function deriveSort(display) {
    const s = (display || '').trim().toLowerCase();
    if (!s) return null;
    const ym = s.match(/\b(?:19|20)\d{2}\b/);
    if (!ym) return null;
    const mm = s.match(/^([a-z]+|\d{1,2})[\s/.]/);
    const month = (mm && MA[mm[1]]) ? MN[MA[mm[1]]] : null;
    return month ? `${ym[0]}-${String(month).padStart(2, '0')}-15` : `${ym[0]}-06-15`;
  }
  document.getElementById('f-date').addEventListener('input', e => {
    const v = deriveSort(e.target.value);
    if (v) document.getElementById('f-sort').value = v;
  });

  // Dropzone
  const dz = document.getElementById('dropzone');
  const fileInput = document.getElementById('file-input');
  dz.addEventListener('click', () => fileInput.click());
  fileInput.addEventListener('change', () => { uploadFiles(fileInput.files); fileInput.value = ''; });
  ['dragenter', 'dragover'].forEach(ev => dz.addEventListener(ev, e => { e.preventDefault(); dz.classList.add('dragover'); }));
  ['dragleave', 'drop'].forEach(ev => dz.addEventListener(ev, e => { e.preventDefault(); dz.classList.remove('dragover'); }));
  dz.addEventListener('drop', e => uploadFiles(e.dataTransfer.files));

  load();
</script>
</body>
</html>"""


# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=5111)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()

    print(f"zhexi.info CMS running at http://{args.host}:{args.port}")
    print(f"Editing: {DEFAULT_DATA.resolve()}")
    print(f"Images:  {IMAGES_DIR.resolve()}")
    print(f"Backups: {BACKUP_DIR.resolve()}")
    app.run(host=args.host, port=args.port, debug=False)


if __name__ == "__main__":
    main()
