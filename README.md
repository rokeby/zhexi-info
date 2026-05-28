# A tiny YAML-driven portfolio site

A one-page, filterable, dated portfolio/CV timeline driven by a single YAML
file, with a small local CMS for editing and a build step that bakes the data
into a static `index.html`.

It's deliberately low-tech: **no framework, no database, no build pipeline to
deploy** — just one HTML file you host anywhere (GitHub Pages, Netlify, an S3
bucket, a USB stick). Python is only used *locally* to edit/generate the page.

This README explains how to **fork it for your own site with your own design**.
You don't have to keep this site's aesthetic — only a thin "contract" of marker
comments, element IDs, and data attributes needs to survive so the build script
and the front-end JS keep working.

---

## How it works

```
entries.yaml          ← single source of truth (content + dates + tags)
   │
   │  python build_site.py        (or the “build site” button in the CMS)
   ▼
index.html            ← static page you deploy; build only rewrites
                        the marker-delimited blocks, leaving your
                        markup/CSS/JS untouched
   ▲
   │  python site_cms.py          (optional local web editor, localhost:5111)
   ▼
entries.yaml
```

- **`entries.yaml`** — all content: a `site` block (heading / news / bio), a
  list of research `themes`, and the `entries` (timeline items).
- **`build_site.py`** — reads the YAML, sorts entries newest-first, groups them
  by year, and writes the result *into specific regions of `index.html`* marked
  by HTML comments. It never touches anything outside those regions.
- **`site_cms.py`** — an optional Flask app (`http://localhost:5111`) for
  editing the YAML in a browser: add/edit/bulk-edit entries, manage themes,
  drag-and-drop images, edit the heading/news/bio, and run the build.
- **`index.html`** — the only file you deploy. It contains your design, plus a
  small `<script>` that powers filtering and the fullscreen image mode.

---

## Quick start (fork from scratch)

```bash
# 1. copy these four files into a new folder:
#      build_site.py   site_cms.py   requirements.txt   entries.yaml
#    (and create an empty  images/  folder)
mkdir my-site && cd my-site
mkdir images

# 2. set up Python (3.10+)
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt   # flask + pyyaml

# 3. start the CMS and add some entries
.venv/bin/python site_cms.py                 # → http://localhost:5111

# 4. build the page
.venv/bin/python build_site.py               # writes index.html

# 5. deploy index.html (+ images/ + any files/ you link to) anywhere static
```

Start `entries.yaml` minimal — the CMS will fill the rest:

```yaml
site:
  heading_html: "<em>My Name</em>"
  news: []
  bio_html: "A sentence about me."
themes: []
entries: []
```

Recommended `.gitignore`:

```
.venv/
.site_backups/
__pycache__/
*.pyc
.DS_Store
```

---

## The data file (`entries.yaml`)

```yaml
site:
  heading_html: "<em>Under Construction</em><br><br>***"   # inline HTML
  news:                                                    # list of HTML strings
    - "Working on a new book."
    - 'Joined <a href="https://example.org">Somewhere</a> as a thing.'
  bio_html: "Multi-paragraph bio. Use <br><br> between paragraphs."
  affiliations:                                            # list of HTML strings
    - 'Resident at <a href="https://example.org">Somewhere</a>.'
  links:                                                   # list of HTML strings
    - '<a href="mailto:me@example.org">email</a>'
  show:                          # per-module visibility (omit → defaults true)
    heading: true
    news: true
    bio: true
    affiliations: true
    links: false                 # e.g. hide the links module from the site

gallery:                       # independent artworks for the fullscreen carousel
  - src: images/artwork.jpg    #   (shown BEFORE captioned entry images, in this order)
    caption: "<em>Artwork</em>, 2024. Medium."
    alt: "plain-text alt"
gallery_shuffle: false         # true → carousel randomises order each time it opens

themes:                        # research themes → become a second filter row
  - slug: sinofuturism         # lowercase, url-safe; used in entries + data-attrs
    label: Sinofuturism        # what's shown on buttons

entries:
  - id: 1                      # unique integer (the CMS assigns these)
    date: "sept 2025"          # DISPLAY string — free text, shown verbatim
    sort: "2025-09-15"         # YYYY-MM-DD — used only for ordering/grouping
    visible: true              # false → excluded from the built page
    types: [research, talk]    # one or more; the FIRST is the visible label
    themes: [sinofuturism]     # theme slugs (must exist in `themes:` above)
    description_html: |-
      co-curated <em>Show</em> at <a href="https://x.org">Venue</a>.
    images:                    # optional documentation images for this entry
      - src: images/foo.jpg
        caption: "<em>Work Title</em>, 2025. Medium."   # art caption (HTML)
        alt: "plain-text alt"                            # accessibility
        # NOTE: an entry image only appears in the carousel if it has a caption.
    created: "2025-09-01T10:00:00"   # timestamps (the CMS maintains these)
    modified: "2025-09-01T10:00:00"
```

Key points:

- **`date` vs `sort`**: `date` is the human label shown on the page; `sort` is
  the real date used to order and to group entries under year headings. The CMS
  derives `sort` from `date` automatically (you can override it). The build
  strips the year from `date` when rendering (the year heading already shows
  it), so `"sept 2025"` displays as `sept`.
- **`types`**: the allowed set is defined in three places (see *Customizing
  types* below). The first type is the label shown on the entry.
- **`visible: false`** hides an entry from the built site but keeps it in the
  data (toggle with the 👁 button in the CMS).
- Editing the YAML through the CMS **strips comments** (PyYAML limitation), so
  keep notes elsewhere.

---

## Designing your own `index.html`

This is the part you fully own. Rewrite the markup and CSS however you like —
the build script and JS only depend on a small contract:

### 1. Build markers (required)

`build_site.py` finds these HTML comment pairs and replaces *only what's between
them*. Put each pair wherever it belongs in your layout:

```html
<!-- TYPE_FILTERS_START --> ... <!-- TYPE_FILTERS_END -->   (inside a <nav>; gets <button>s)
<!-- THEME_FILTERS_START --> ... <!-- THEME_FILTERS_END --> (inside a <nav>; gets <button>s)
<!-- TIMELINE_START -->  ... <!-- TIMELINE_END -->          (inside a <ul>/<ol>; gets <li>s)
```

The five **site-content modules** use *section-level* markers — the build emits
the whole section (its `<h2>` heading + list/text), or nothing when the module
is hidden (`site.show.<module> = false`) or empty. So wrap an empty span where
each should appear:

```html
<!-- HEADING_START --><!-- HEADING_END -->            → <h2 id="header">…</h2>
<!-- NEWS_START --><!-- NEWS_END -->                  → <h2>News</h2><ul id="news">…</ul>
<!-- BIO_START --><!-- BIO_END -->                    → <h2>Bio</h2><ul><li id="bio">…</li></ul>
<!-- AFFILIATIONS_START --><!-- AFFILIATIONS_END -->  → <h2>Affiliations</h2><ul id="affiliations">…</ul>
<!-- LINKS_START --><!-- LINKS_END -->                → <h2>Links</h2><ul id="links">…</ul>
```

The timeline (`TIMELINE_START`) is the only required block. Rename the `<h2>`
labels or restyle freely — the build re-emits them, so change the wording in
`build_site.py`'s render functions if you want different headings.

### 2. What the build writes (style these as you wish)

- **Timeline entries:**
  ```html
  <li data-type="research talk" data-themes="sinofuturism" data-id="3">
    <span class="date">sept</span><span class="type">research</span>description…
  </li>
  ```
  Class names `date` / `type` and the `data-*` attributes are fixed; everything
  about how they look is up to your CSS.
- **Carousel data** — a JSON list the build assembles (gallery bucket + captioned
  entry images from visible entries):
  ```html
  <script type="application/json" id="gallery-data">[{"src":"…","caption":"…","alt":"…"}]</script>
  ```
- **Year headings** (inserted between years):
  ```html
  <li class="year-heading" data-year="2025">2025</li>
  ```
- **Filter buttons:**
  ```html
  <button data-type-filter="all" class="active">all</button>
  <button data-type-filter="research">research</button>
  <!-- theme row, same shape with data-theme-filter -->
  ```

### 3. Required IDs/hooks for the front-end JS

The `<script>` at the bottom of `index.html` provides **filtering** and
**image mode**. Keep these IDs/selectors (or rewrite the script to match your
own markup):

| Hook | Used for |
|---|---|
| `#type-filters button[data-type-filter]` | type filter buttons |
| `#theme-filters` + `button[data-theme-filter]` | theme filter row (auto-hides when no themes apply to the current type) |
| `#timeline` containing the `<li>`s | the list being filtered |
| `.dim` class | how a filtered-out entry is hidden (this site uses `display:none`) |
| `#mode-toggle` | button that toggles image mode |
| `#image-mode` + `#image-mode-img`, `-placeholder`, `-desc`, `-prev`, `-next` | the fullscreen image viewer |
| `#gallery-data` | the `<script type="application/json">` the carousel reads its slides from |

The filtering logic dims any `#timeline > li` whose `data-type` doesn't include
the active type **and** whose `data-themes` doesn't include the active theme.
Image mode parses `#gallery-data` (a build-generated JSON list of
`{src, caption, alt}`) and shows one fullscreen slide per item, with the
caption bottom-right. The list is the curated `gallery` bucket followed by every
captioned image on a visible entry.

If you don't want image mode, delete the `#mode-toggle` / `#image-mode` markup
and that IIFE. If you don't want themes, leave `themes: []` and the theme row
stays hidden.

### 4. Minimal skeleton to start from

```html
<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><title>My Site</title>
  <style>/* your design here */ .dim { display: none; }</style>
</head>
<body>
  <!-- HEADING_START --><!-- HEADING_END -->
  <!-- NEWS_START --><!-- NEWS_END -->

  <nav id="type-filters"><!-- TYPE_FILTERS_START --><!-- TYPE_FILTERS_END --></nav>
  <nav id="theme-filters"><!-- THEME_FILTERS_START --><!-- THEME_FILTERS_END --></nav>

  <ul id="timeline"><!-- TIMELINE_START --><!-- TIMELINE_END --></ul>

  <!-- BIO_START --><!-- BIO_END -->
  <!-- AFFILIATIONS_START --><!-- AFFILIATIONS_END -->
  <!-- LINKS_START --><!-- LINKS_END -->

  <script>
    // minimal type+theme filter
    (function () {
      const items = document.querySelectorAll('#timeline > li');
      let type = 'all', theme = 'all';
      function apply() {
        items.forEach(li => {
          if (li.classList.contains('year-heading')) return;
          const t = (li.dataset.type || '').split(/\s+/);
          const th = (li.dataset.themes || '').split(/\s+/);
          const ok = (type === 'all' || t.includes(type)) &&
                     (theme === 'all' || th.includes(theme));
          li.classList.toggle('dim', !ok);
        });
      }
      document.querySelectorAll('#type-filters button').forEach(b =>
        b.onclick = () => { type = b.dataset.typeFilter;
          document.querySelectorAll('#type-filters button').forEach(x => x.classList.toggle('active', x === b));
          apply(); });
      document.querySelectorAll('#theme-filters button').forEach(b =>
        b.onclick = () => { theme = b.dataset.themeFilter;
          document.querySelectorAll('#theme-filters button').forEach(x => x.classList.toggle('active', x === b));
          apply(); });
    })();
  </script>
</body>
</html>
```

Run `python build_site.py` and the markers fill in. Grow the script from there
(year-heading handling, dynamic theme availability, image mode) by copying the
relevant pieces from this repo's `index.html` if you want them.

---

## The CMS (`site_cms.py`)

```bash
.venv/bin/python site_cms.py            # http://localhost:5111
.venv/bin/python site_cms.py --port 8080
```

- Browse / search / sort entries (by timeline date, type, date added/modified).
- Add / edit / delete entries; toggle visibility (👁); bulk-select and
  bulk add/remove types & themes, bulk show/hide/delete.
- Manage research themes (add, rename — renames propagate to all entries, delete).
- Drag-and-drop images into an entry (saved to `images/`); multiple per entry,
  each with an art-historical caption + alt.
- **Gallery panel** — curate the image carousel: drag-drop independent artworks,
  caption each (HTML, italic title), reorder (↑/↓), remove, and a **randomise
  order** toggle. The carousel shows these first, then captioned entry images.
- Edit the site modules — heading, news, bio, affiliations, links — each with a
  show/hide checkbox (unticking removes that whole section from the built page).
- **Build site** button runs `build_site.py` and shows what changed.

Every save writes a timestamped backup to `.site_backups/`. The CMS never
deploys — it only edits `entries.yaml` and (re)builds `index.html`.

---

## Building & deploying

```bash
.venv/bin/python build_site.py            # rebuild index.html
.venv/bin/python build_site.py --check    # validate entries.yaml only, no write
```

The build prints a per-section summary of what changed. Deploy the static
files: `index.html`, the `images/` folder, plus any `files/` (PDFs etc.) you
link to, and your `CNAME`/favicon if you have them. No server runtime needed.

---

## Customizing the types

The activity types (`exhibition`, `writing`, …) live in **three** spots — keep
them in sync if you change them:

1. `build_site.py` → `TYPE_ORDER` (order of the filter buttons + validation)
2. `site_cms.py` → `ALLOWED_TYPES` (server-side validation)
3. `site_cms.py` → the `ALLOWED_TYPES` constant inside the embedded CMS `<script>`

Themes, by contrast, are fully data-driven (defined in `entries.yaml`/the CMS)
and need no code changes.

---

## File reference

| File | Purpose |
|---|---|
| `entries.yaml` | single source of truth (site content, themes, entries) |
| `index.html` | the deployed page; your design + filled-in marker blocks |
| `build_site.py` | YAML → `index.html` (marker-block renderer + change report) |
| `site_cms.py` | local Flask editor (`localhost:5111`) |
| `requirements.txt` | `flask`, `pyyaml` |
| `images/` | uploaded entry + gallery images |
| `.site_backups/` | timestamped YAML backups (gitignored) |

---

## Requirements

- Python 3.10+
- `flask`, `pyyaml` (`pip install -r requirements.txt`)
- A static host for `index.html` (nothing server-side at runtime)
