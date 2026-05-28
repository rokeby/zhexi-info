#!/usr/bin/env python3
"""
build_site.py — render entries.yaml into the timeline block of index.html.

Reads entries.yaml (single source of truth), sorts entries by `sort` date
(newest first), and rewrites three marker-delimited blocks in index.html:

    <!-- TIMELINE_START -->          ...timeline <li>s...     <!-- TIMELINE_END -->
    <!-- TYPE_FILTERS_START -->      ...type buttons...        <!-- TYPE_FILTERS_END -->
    <!-- THEME_FILTERS_START -->     ...theme buttons...       <!-- THEME_FILTERS_END -->

Usage:
    python build_site.py                  # build in place
    python build_site.py --check          # validate yaml + print stats, no write
    python build_site.py --data foo.yaml  # custom data file
    python build_site.py --html foo.html  # custom HTML target
"""

import argparse
import html
import json
import re
import sys
from pathlib import Path

# `html` is the stdlib module (used for escaping); avoid shadowing it.

try:
    import yaml
except ImportError:
    sys.exit("PyYAML not found. Install it: pip install pyyaml")

# ─── Config ───────────────────────────────────────────────────────────────────

DEFAULT_DATA = Path("entries.yaml")
DEFAULT_HTML = Path("index.html")

# Filter types, in display order. The first is the default-active button ("all").
TYPE_ORDER = ["exhibition", "writing", "research", "talk", "teaching"]


# ─── Rendering ────────────────────────────────────────────────────────────────

def render_type_filters() -> str:
    lines = ['        <button data-type-filter="all" class="active">all</button>']
    for t in TYPE_ORDER:
        lines.append(f'        <button data-type-filter="{t}">{t}</button>')
    return "\n".join(lines)


def render_theme_filters(themes: list) -> str:
    if not themes:
        return ""  # build leaves the block empty; CSS hides the nav
    lines = [
        '        <button data-theme-filter="all" class="active">~</button>',
    ]
    for theme in themes:
        slug = theme["slug"]
        label = theme.get("label", slug)
        lines.append(f'        <button data-theme-filter="{slug}">{label}</button>')
    return "\n".join(lines)


def short_date(date_str: str, section_year: str) -> str:
    """Strip the section's year from a date display string.

    "2025" + year=2025  -> ""           (year heading already shows 2025)
    "sept 2025" + year=2025 -> "sept"
    "apr-jul 2025" + year=2025 -> "apr-jul"
    "2022-23" + year=2023 -> "2022-23"  (preserved; conveys span)
    """
    if not date_str:
        return ""
    s = re.sub(rf"\b{re.escape(section_year)}(?:-\d{{2,4}})?\b", "", date_str)
    s = re.sub(r"^[\s,]+|[\s,]+$", "", s).strip()
    s = re.sub(r"\s+", " ", s)
    return s


def get_images(entry: dict) -> list:
    """Normalize an entry's images to a list of {src, alt}, handling the
    legacy single-image schema (image / image_alt)."""
    out = []
    for im in entry.get("images") or []:
        if isinstance(im, str) and im.strip():
            out.append({"src": im.strip(), "alt": "", "caption": ""})
        elif isinstance(im, dict) and (im.get("src") or "").strip():
            out.append({
                "src": im["src"].strip(),
                "alt": (im.get("alt") or "").strip(),
                "caption": (im.get("caption") or "").strip(),
            })
    if not out and entry.get("image"):
        out.append({"src": entry["image"], "alt": entry.get("image_alt") or "", "caption": ""})
    return out


def render_entry(entry: dict, section_year: str) -> str:
    types = entry.get("types") or []
    if not types:
        raise ValueError(f"Entry id={entry.get('id')} has no types")

    primary_type = types[0]
    data_type = " ".join(types)
    themes = entry.get("themes") or []
    data_themes = " ".join(themes)
    date_display = short_date(entry.get("date", ""), section_year)
    desc = (entry.get("description_html") or "").strip()
    entry_id = entry.get("id", "")

    attrs = [f'data-type="{data_type}"']
    if data_themes:
        attrs.append(f'data-themes="{data_themes}"')
    if entry_id != "":
        attrs.append(f'data-id="{entry_id}"')

    return (
        f'        <li {" ".join(attrs)}>'
        f'<span class="date">{date_display}</span>'
        f'<span class="type">{primary_type}</span>'
        f'{desc}</li>'
    )


def render_timeline(entries: list) -> str:
    # Only visible entries are rendered.
    visible = [e for e in entries if e.get("visible", True)]

    # Sort by sort date descending; entries with same sort date keep YAML order.
    def sort_key(e):
        return (str(e.get("sort", "")), -int(e.get("id", 0) or 0))
    sorted_entries = sorted(visible, key=sort_key, reverse=True)

    out = []
    last_year = None
    for e in sorted_entries:
        sort_val = str(e.get("sort", ""))
        year = sort_val[:4] if len(sort_val) >= 4 else ""
        if year and year != last_year:
            out.append(f'        <li class="year-heading" data-year="{year}">{year}</li>')
            last_year = year
        out.append(render_entry(e, year))
    return "\n".join(out)


def _shown(site: dict, key: str) -> bool:
    return bool((site.get("show") or {}).get(key, True))


def _li_list(items) -> str:
    return "\n".join(f"        <li>{(x or '').strip()}</li>" for x in (items or []) if (x or "").strip())


def render_heading(site: dict) -> str:
    if not _shown(site, "heading"):
        return ""
    return f'<h2 id="header">{(site.get("heading_html") or "").strip()}</h2>'


def render_news(site: dict) -> str:
    if not _shown(site, "news") or not (site.get("news") or []):
        return ""
    return f'<h2>News</h2>\n      <ul id="news">\n{_li_list(site.get("news"))}\n      </ul>'


def render_bio(site: dict) -> str:
    if not _shown(site, "bio") or not (site.get("bio_html") or "").strip():
        return ""
    return (f'<h2>Bio</h2>\n      <ul>\n        '
            f'<li id="bio">{(site.get("bio_html") or "").strip()}</li>\n      </ul>')


def render_affiliations(site: dict) -> str:
    if not _shown(site, "affiliations") or not (site.get("affiliations") or []):
        return ""
    return f'<h2>Affiliations</h2>\n      <ul id="affiliations">\n{_li_list(site.get("affiliations"))}\n      </ul>'


def render_links(site: dict) -> str:
    if not _shown(site, "links") or not (site.get("links") or []):
        return ""
    return f'<h2>Links</h2>\n      <ul id="links">\n{_li_list(site.get("links"))}\n      </ul>'


def render_gallery(gallery: list, entries: list, shuffle: bool = False) -> str:
    """Build the image-carousel data: the curated gallery bucket first, then
    every captioned image on a *visible* entry. Emitted as a JSON <script>;
    data-shuffle tells the viewer to randomise the order on open."""
    slides = []
    for g in gallery or []:
        src = (g.get("src") or "").strip() if isinstance(g, dict) else ""
        if src:
            slides.append({"src": src,
                           "caption": (g.get("caption") or "").strip(),
                           "alt": (g.get("alt") or "").strip()})
    for e in entries:
        if not e.get("visible", True):
            continue
        for im in get_images(e):
            if im.get("caption"):
                slides.append({"src": im["src"], "caption": im["caption"], "alt": im.get("alt", "")})
    payload = json.dumps(slides, ensure_ascii=False).replace("</", "<\\/")
    flag = "true" if shuffle else "false"
    return f'<script type="application/json" id="gallery-data" data-shuffle="{flag}">{payload}</script>'


# ─── Block replacement ───────────────────────────────────────────────────────

def replace_block(html: str, start_marker: str, end_marker: str, new_content: str) -> str:
    """Replace text between two HTML comment markers (markers preserved)."""
    pattern = re.compile(
        r"(<!-- " + re.escape(start_marker) + r" -->)"
        r"(.*?)"
        r"(<!-- " + re.escape(end_marker) + r" -->)",
        re.DOTALL,
    )

    if not pattern.search(html):
        raise RuntimeError(f"Markers {start_marker}/{end_marker} not found in HTML")

    # Wrap new content with newlines for readability
    replacement = r"\1" + "\n" + new_content + "\n        " + r"\3"
    return pattern.sub(replacement, html, count=1)


# ─── Change reporting ────────────────────────────────────────────────────────

def _block_text(doc: str, start: str, end: str) -> str:
    m = re.search(
        re.escape(f"<!-- {start} -->") + r"(.*?)" + re.escape(f"<!-- {end} -->"),
        doc, re.DOTALL,
    )
    return m.group(1).strip() if m else ""


def _strip_tags(s: str) -> str:
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def report_changes(original: str, new: str) -> None:
    """Print a readable summary of what changed in each content block.

    Compares line *sets* so re-ordering (e.g. a sort-date tweak) doesn't show
    as noise — only genuinely new/removed/edited lines are reported.
    """
    sections = [
        ("heading", "HEADING_START", "HEADING_END"),
        ("news", "NEWS_START", "NEWS_END"),
        ("type filters", "TYPE_FILTERS_START", "TYPE_FILTERS_END"),
        ("theme filters", "THEME_FILTERS_START", "THEME_FILTERS_END"),
        ("timeline", "TIMELINE_START", "TIMELINE_END"),
        ("bio", "BIO_START", "BIO_END"),
        ("affiliations", "AFFILIATIONS_START", "AFFILIATIONS_END"),
        ("links", "LINKS_START", "LINKS_END"),
    ]
    any_change = False
    for name, s, e in sections:
        old_lines = [l for l in _block_text(original, s, e).splitlines() if l.strip()]
        new_lines = [l for l in _block_text(new, s, e).splitlines() if l.strip()]
        old_set, new_set = set(old_lines), set(new_lines)
        added = [_strip_tags(l) for l in new_lines if l not in old_set]
        removed = [_strip_tags(l) for l in old_lines if l not in new_set]
        added = [l for l in added if l]
        removed = [l for l in removed if l]
        if not added and not removed:
            continue
        any_change = True
        print(f"\nchanges in {name}: +{len(added)} / -{len(removed)}")
        for l in removed[:25]:
            print(f"  - {l[:100]}")
        for l in added[:25]:
            print(f"  + {l[:100]}")
        extra = max(len(added), len(removed)) - 25
        if extra > 0:
            print(f"  … (+{extra} more)")

    if not any_change:
        print("\nno content changes — index.html already up to date.")


# ─── Validation ──────────────────────────────────────────────────────────────

def validate(data: dict) -> list[str]:
    """Return a list of human-readable validation problems."""
    problems = []
    entries = data.get("entries", [])
    themes_defined = {t["slug"] for t in (data.get("themes") or []) if "slug" in t}

    seen_ids = set()
    for i, e in enumerate(entries):
        prefix = f"entry index {i}"
        eid = e.get("id")
        if eid is None:
            problems.append(f"{prefix}: missing id")
        elif eid in seen_ids:
            problems.append(f"{prefix}: duplicate id {eid}")
        else:
            seen_ids.add(eid)

        types = e.get("types") or []
        if not types:
            problems.append(f"id={eid}: no types listed")
        for t in types:
            if t not in TYPE_ORDER:
                problems.append(f"id={eid}: unknown type '{t}' (allowed: {TYPE_ORDER})")

        for theme in (e.get("themes") or []):
            if theme not in themes_defined:
                problems.append(f"id={eid}: theme '{theme}' not defined in top-level themes")

        if not e.get("date"):
            problems.append(f"id={eid}: missing display date")
        if not e.get("sort"):
            problems.append(f"id={eid}: missing sort date")
        if not (e.get("description_html") or "").strip():
            problems.append(f"id={eid}: empty description_html")

    return problems


# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", default=DEFAULT_DATA, type=Path, help="YAML data file")
    ap.add_argument("--html", default=DEFAULT_HTML, type=Path, help="HTML target file")
    ap.add_argument("--check", action="store_true", help="Validate only; don't write HTML")
    args = ap.parse_args()

    if not args.data.exists():
        sys.exit(f"Data file not found: {args.data}")
    if not args.html.exists():
        sys.exit(f"HTML file not found: {args.html}")

    data = yaml.safe_load(args.data.read_text(encoding="utf-8")) or {}
    entries = data.get("entries") or []
    themes = data.get("themes") or []
    site = data.get("site") or {}
    gallery = data.get("gallery") or []
    gallery_shuffle = bool(data.get("gallery_shuffle"))

    problems = validate(data)
    if problems:
        print("Validation problems:", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        if args.check:
            sys.exit(1)
        print(f"\nContinuing with {len(problems)} warning(s).", file=sys.stderr)

    n_visible = sum(1 for e in entries if e.get("visible", True))
    print(f"Loaded {len(entries)} entries ({n_visible} visible), {len(themes)} theme(s).")

    if args.check:
        return

    original = args.html.read_text(encoding="utf-8")
    doc = original
    doc = replace_block(doc, "TYPE_FILTERS_START", "TYPE_FILTERS_END", render_type_filters())
    doc = replace_block(doc, "THEME_FILTERS_START", "THEME_FILTERS_END", render_theme_filters(themes))
    doc = replace_block(doc, "TIMELINE_START", "TIMELINE_END", render_timeline(entries))

    # Site content modules — each renders the full section (heading + list),
    # or empty when hidden via site.show / no content.
    doc = replace_block(doc, "HEADING_START", "HEADING_END", render_heading(site))
    doc = replace_block(doc, "NEWS_START", "NEWS_END", render_news(site))
    doc = replace_block(doc, "BIO_START", "BIO_END", render_bio(site))
    doc = replace_block(doc, "AFFILIATIONS_START", "AFFILIATIONS_END", render_affiliations(site))
    doc = replace_block(doc, "LINKS_START", "LINKS_END", render_links(site))
    doc = replace_block(doc, "GALLERY_START", "GALLERY_END", render_gallery(gallery, entries, gallery_shuffle))

    report_changes(original, doc)

    args.html.write_text(doc, encoding="utf-8")
    print(f"\nWrote {args.html}")


if __name__ == "__main__":
    main()
