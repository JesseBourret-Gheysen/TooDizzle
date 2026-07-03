"""Instagram enrichment for TooDizzle todos.

When a todo's text contains a link to a public Instagram post, we fetch the
post's title, caption (description) and cover image, run OCR over the image to
pull out any text baked into the picture, and fold all of that back into the
todo's ``text`` so it becomes readable and searchable inside the app. A per-row
``enriched`` flag records that this has already happened so a post is never
processed twice.

Why the Open Graph approach (not instaloader / the private GraphQL API):
Instagram now answers anonymous ``graphql/query`` calls with 403, so the usual
scraping libraries fail without a logged-in session. The public post page,
however, still serves Open Graph / ``<meta>`` tags to crawlers (this is what
link-preview cards use). Fetching that page with a crawler User-Agent gives us
``og:title`` (owner + caption snippet), ``meta[name=description]`` (the fullest
caption Instagram exposes) and ``og:image`` (a downloadable cover JPEG) with no
credentials at all. Carousels only expose their cover image this way, which is
an accepted limitation.

Everything here is slow (network + OCR) so it runs on a single background worker
thread fed by a queue -- never on the request path, and only one fetch at a time
so we stay polite to Instagram.
"""

import html as _html
import os
import queue
import re
import threading
import time
import urllib.request
from datetime import datetime

# --- Config, wired up by app via init() -------------------------------------
_get_row = None       # callable(item_id) -> row dict or None
_update_row = None    # callable(item_id, {field: value, ...})
_image_root = None    # dir under /data where cover images are stored
_started = False

ENABLED = os.environ.get('ENRICH_ENABLED', '1') not in ('0', 'false', 'False', '')

# Crawler UA -- Instagram serves the Open Graph meta tags to link-preview bots.
_UA = ('Mozilla/5.0 (compatible; facebookexternalhit/1.1; '
       '+http://www.facebook.com/externalhit_uatext.php)')
_HEADERS = {'User-Agent': _UA, 'Accept-Language': 'en-US,en;q=0.9'}

# Detect an Instagram post / reel / tv link and capture its shortcode.
INSTAGRAM_RE = re.compile(
    r'https?://(?:www\.)?instagram\.com/(?:[^/\s?#]+/)?(?:p|reel|reels|tv)/'
    r'([A-Za-z0-9_-]+)',
    re.IGNORECASE,
)

# Sentinel comments bracketing the block we append, so re-enrichment replaces
# the old block instead of stacking duplicates. HTML comments render invisibly
# through the app's markdown pipeline.
_MARK_START = '<!--IG-ENRICH-->'
_MARK_END = '<!--/IG-ENRICH-->'
_BLOCK_RE = re.compile(r'\n*' + re.escape(_MARK_START) + r'.*?' + re.escape(_MARK_END),
                       re.DOTALL)

_MAX_OCR_CHARS = 4000   # guard the CSV against a runaway OCR dump

# Delay between successive Instagram fetches (be polite / dodge rate limits).
_FETCH_GAP_SECS = float(os.environ.get('ENRICH_FETCH_GAP_SECS', '3'))

_queue = queue.Queue()
_inflight = set()
_inflight_lock = threading.Lock()


# --- Public API -------------------------------------------------------------

def init(get_row, update_row, image_root):
    """Wire the worker to the app's CSV accessors and start the background
    thread (idempotent). ``get_row``/``update_row`` must do their own locking."""
    global _get_row, _update_row, _image_root, _started
    _get_row = get_row
    _update_row = update_row
    _image_root = image_root
    if not _started and ENABLED:
        _started = True
        threading.Thread(target=_worker, name='ig-enrich', daemon=True).start()


def extract_shortcode(text):
    """Return the Instagram shortcode in ``text``, or None."""
    m = INSTAGRAM_RE.search(text or '')
    return m.group(1) if m else None


def needs_enrichment(row):
    """True if the row links an Instagram post and hasn't been enriched yet."""
    return bool(extract_shortcode(row.get('text', ''))) and row.get('enriched') != 'yes'


def maybe_enqueue(row):
    """Queue a freshly-added todo for enrichment if it qualifies. Safe no-op
    when disabled or when the row has no Instagram link."""
    if not (ENABLED and _started):
        return False
    if not needs_enrichment(row):
        return False
    return _enqueue(row['id'])


def scan(rows):
    """Queue every un-enriched Instagram todo. Errored rows are retried.
    Returns the number queued."""
    if not (ENABLED and _started):
        return 0
    n = 0
    for row in rows:
        if needs_enrichment(row) and _enqueue(row['id']):
            n += 1
    return n


# --- Internals --------------------------------------------------------------

def _enqueue(item_id):
    with _inflight_lock:
        if item_id in _inflight:
            return False
        _inflight.add(item_id)
    _queue.put(item_id)
    return True


def _worker():
    while True:
        item_id = _queue.get()
        try:
            _process(item_id)
        except Exception:
            try:
                _update_row(item_id, {'enriched': 'error'})
            except Exception:
                pass
        finally:
            with _inflight_lock:
                _inflight.discard(item_id)
            _queue.task_done()
            time.sleep(_FETCH_GAP_SECS)


def _process(item_id):
    row = _get_row(item_id)
    if not row or row.get('enriched') == 'yes':
        return
    shortcode = extract_shortcode(row.get('text', ''))
    if not shortcode:
        return
    data = _fetch_post(shortcode)
    ocr_text = _ocr_image(data.get('image_path')) if data.get('image_path') else ''
    block = _render_block(data, ocr_text)
    new_text = _BLOCK_RE.sub('', row.get('text', '')).rstrip() + '\n\n' + block
    _update_row(item_id, {'text': new_text, 'enriched': 'yes'})


def _fetch_post(shortcode):
    """Fetch the post page and pull title / description / cover image. Downloads
    the cover to ``_image_root/<shortcode>/cover.jpg``. Returns a dict; raises on
    a hard network failure so the caller marks the row 'error'."""
    url = 'https://www.instagram.com/p/%s/' % shortcode
    req = urllib.request.Request(url, headers=_HEADERS)
    page = urllib.request.urlopen(req, timeout=25).read().decode('utf-8', 'replace')

    title = _meta(page, 'property', 'og:title')
    # meta[name=description] is the fullest caption IG exposes; og:description is
    # a shorter fallback.
    caption = (_meta(page, 'name', 'description')
               or _meta(page, 'property', 'og:description') or '')
    image_url = _meta(page, 'property', 'og:image')

    handle, date = _parse_description(caption)
    image_path = None
    if image_url:
        try:
            image_path = _download_image(shortcode, image_url)
        except Exception:
            image_path = None

    return {
        'shortcode': shortcode,
        'url': url,
        'title': title,
        'caption': caption,
        'handle': handle,
        'date': date,
        'image_path': image_path,
    }


def _meta(page, attr, key):
    """Extract and HTML-unescape a <meta {attr}="{key}" content="..."> value."""
    m = re.search(r'<meta %s="%s" content="([^"]*)"' % (attr, re.escape(key)), page)
    return _html.unescape(m.group(1)) if m else ''


def _parse_description(caption):
    """From 'N likes, M comments - handle on Mon DD, YYYY: "..."' pull the
    handle and date. Best-effort; returns ('', '') if the shape differs."""
    m = re.search(r'-\s*(\S+)\s+on\s+([A-Za-z]+ \d{1,2}, \d{4})', caption)
    if m:
        return m.group(1), m.group(2)
    return '', ''


def _download_image(shortcode, image_url):
    d = os.path.join(_image_root, shortcode)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, 'cover.jpg')
    req = urllib.request.Request(image_url, headers=_HEADERS)
    data = urllib.request.urlopen(req, timeout=25).read()
    with open(path, 'wb') as f:
        f.write(data)
    return path


def _ocr_image(path):
    """Run tesseract over the image and return cleaned text ('' on any failure
    or if the deps/binary are missing -- enrichment still proceeds)."""
    try:
        import pytesseract
        from PIL import Image
    except Exception:
        return ''
    try:
        with Image.open(path) as img:
            raw = pytesseract.image_to_string(img)
    except Exception:
        return ''
    # Collapse whitespace, drop empty lines, cap length.
    lines = [ln.strip() for ln in raw.splitlines()]
    text = '\n'.join(ln for ln in lines if ln)
    text = re.sub(r'[ \t]{2,}', ' ', text).strip()
    return text[:_MAX_OCR_CHARS]


def _render_block(data, ocr_text):
    """Build the markdown block appended to the todo's text."""
    ts = datetime.now().strftime('%Y-%m-%d %H:%M')
    who = data.get('handle') or ''
    when = data.get('date') or ''
    header = '📷 **Instagram post**'
    if who:
        header += ' — @%s' % who
    if when:
        header += ' · %s' % when

    parts = [_MARK_START, '---', header, '']

    title = (data.get('title') or '').strip()
    if title:
        parts.append('**Title:** %s' % title)
        parts.append('')

    caption = (data.get('caption') or '').strip()
    if caption:
        parts.append('**Description:**')
        parts.append('')
        parts.append(caption)
        parts.append('')

    if data.get('image_path'):
        parts.append('![Instagram cover](/instagram-image/%s/cover.jpg)'
                     % data['shortcode'])
        parts.append('')

    parts.append('**Text found in image:**')
    parts.append('')
    parts.append(ocr_text if ocr_text else '_(no text detected)_')
    parts.append('')
    parts.append('_Enriched %s._' % ts)
    parts.append(_MARK_END)
    return '\n'.join(parts)
