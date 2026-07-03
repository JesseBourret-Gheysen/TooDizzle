import csv
import json
import os
import re
import uuid
import threading
import urllib.request
from datetime import datetime
from urllib.parse import urlparse

import markupsafe
import markdown as _md
from flask import (Flask, render_template, request, redirect, url_for, jsonify,
                   send_from_directory, abort)

import store
from store import InsufficientSpaceError
import enrich

app = Flask(__name__)

CSV_PATH = '/data/todos.csv'
LOCK_PATH = '/data/.todos.lock'
BACKUP_DIR = '/data/backups'
IMAGE_DIR = '/data/instagram'   # downloaded Instagram cover images live here
FIELDNAMES = ['id', 'text', 'type', 'subtype', 'date_added', 'done', 'enriched']
URL_RE = re.compile(r'https?://[^\s<>"]+')

N8N_WEBHOOK_BASE = os.environ.get('N8N_WEBHOOK_BASE', '').rstrip('/')


def _csv_lock():
    """Exclusive lock around every CSV mutation (threading.Lock + fcntl.flock).
    Backup/atomic/prune logic lives in store.py. Not reentrant — never nest it."""
    return store.CsvLock(LOCK_PATH)


def _read_rows_raw():
    """Read all rows, normalized to FIELDNAMES. Caller must hold _csv_lock()."""
    return store.read_rows(CSV_PATH, FIELDNAMES)


def _atomic_write(rows):
    """Atomically replace CSV_PATH with `rows`. Caller must hold _csv_lock().
    Space-checks (raises InsufficientSpaceError → HTTP 507), wipe-guards, takes a
    deduped backup, and prunes to record_count*2. See store.atomic_write."""
    store.atomic_write(CSV_PATH, FIELDNAMES, BACKUP_DIR, rows)


@app.errorhandler(InsufficientSpaceError)
def _handle_insufficient_space(exc):
    return jsonify({'error': 'insufficient storage: mutation refused'}), 507


# --- Instagram enrichment wiring -------------------------------------------
# The enrichment worker (enrich.py) runs off the request path. It reads/updates
# single rows through these lock-holding helpers so it never races the app's own
# CSV mutations.

def _get_task(item_id):
    """Return a copy of the row with `item_id`, or None. Takes the CSV lock."""
    ensure_csv()
    with _csv_lock():
        rows = _read_rows_raw()
    row = next((r for r in rows if r['id'] == item_id), None)
    return dict(row) if row else None


def _update_task(item_id, updates):
    """Apply `updates` (field->value) to the row with `item_id`. Takes the lock."""
    ensure_csv()
    with _csv_lock():
        rows = _read_rows_raw()
        for row in rows:
            if row['id'] == item_id:
                row.update(updates)
                break
        else:
            return
        _atomic_write(rows)


enrich.init(_get_task, _update_task, IMAGE_DIR)


def _fire_webhook(url, payload):
    try:
        body = json.dumps(payload).encode()
        req = urllib.request.Request(
            url, data=body,
            headers={'Content-Type': 'application/json'},
            method='POST'
        )
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass  # never block the app if n8n is down


def send_webhook(path, payload):
    if not N8N_WEBHOOK_BASE:
        return
    threading.Thread(
        target=_fire_webhook,
        args=(f"{N8N_WEBHOOK_BASE}{path}", payload),
        daemon=True
    ).start()


@app.template_filter('linkify')
def linkify_filter(text):
    result = []
    last = 0
    for m in URL_RE.finditer(text):
        result.append(str(markupsafe.escape(text[last:m.start()])))
        url = m.group()
        result.append(
            f'<a href="{markupsafe.escape(url)}" target="_blank" '
            f'rel="noopener noreferrer" class="task-link">'
            f'{markupsafe.escape(url)}</a>'
        )
        last = m.end()
    result.append(str(markupsafe.escape(text[last:])))
    return markupsafe.Markup(''.join(result))


def ensure_csv():
    with _csv_lock():
        if not os.path.exists(CSV_PATH):
            _atomic_write([])
            return
        # Migrate existing CSV if new columns are missing
        with open(CSV_PATH, 'r', newline='') as f:
            existing = csv.DictReader(f).fieldnames or []
        if any(col not in existing for col in FIELDNAMES):
            _atomic_write(_read_rows_raw())


def read_todos():
    ensure_csv()
    with _csv_lock():
        rows = _read_rows_raw()
    for row in rows:
        for col in FIELDNAMES:
            row.setdefault(col, '')
        row['is_instagram'] = bool(enrich.extract_shortcode(row['text']))
        m = URL_RE.search(row['text'])
        if m:
            row['link'] = m.group()
            try:
                p = urlparse(m.group())
                row['source'] = f"{p.scheme}://{p.netloc}"
                netloc = p.netloc
                if netloc.startswith('www.'):
                    netloc = netloc[4:]
                row['display_source'] = netloc
            except Exception:
                row['source'] = ''
                row['display_source'] = ''
        else:
            row['link'] = ''
            row['source'] = ''
            row['display_source'] = ''
        # Markdown-rendered full text for expanded view
        row['text_html'] = markupsafe.Markup(
            _md.markdown(row['text'], extensions=['nl2br'])
        )
        # First-line preview, capped at 120 chars
        first_line = row['text'].split('\n')[0]
        if len(first_line) > 120:
            row['text_preview'] = first_line[:120]
            row['text_truncated'] = True
        else:
            row['text_preview'] = first_line
            row['text_truncated'] = (row['text'] != first_line)
    return rows


@app.route('/')
def index():
    return render_template('input.html')


@app.route('/tasks')
def tasks():
    todos = read_todos()
    todos.reverse()
    return render_template('tasks.html', todos=todos)


@app.route('/scan', methods=['POST'])
def scan():
    """Queue every un-enriched Instagram todo for enrichment. Returns the count
    queued; the actual fetching/OCR happens on the background worker."""
    ensure_csv()
    with _csv_lock():
        rows = _read_rows_raw()
    queued = enrich.scan(rows)
    return jsonify({'queued': queued})


@app.route('/instagram-image/<shortcode>/<path:filename>')
def instagram_image(shortcode, filename):
    """Serve a downloaded Instagram cover image. Both path parts are validated
    so a crafted request can't escape IMAGE_DIR."""
    if not re.fullmatch(r'[A-Za-z0-9_-]+', shortcode) \
            or not re.fullmatch(r'[A-Za-z0-9_.-]+', filename):
        abort(404)
    directory = os.path.join(IMAGE_DIR, shortcode)
    if not os.path.isfile(os.path.join(directory, filename)):
        abort(404)
    return send_from_directory(directory, filename)


@app.route('/submit', methods=['POST'])
def submit():
    text = request.form.get('text', '').strip()
    if not text:
        return redirect(url_for('index'))
    task_id = str(uuid.uuid4())[:8]
    todo_type = request.form.get('type', 'Other').strip()
    date_added = datetime.now().strftime('%Y-%m-%d')
    new_row = {
        'id': task_id,
        'text': text,
        'type': todo_type,
        'subtype': request.form.get('subtype', '').strip() if request.form.get('type') == 'Tech' else '',
        'date_added': date_added,
        'done': '',
        'enriched': '',
    }
    ensure_csv()
    with _csv_lock():
        rows = _read_rows_raw()
        rows.append(new_row)
        _atomic_write(rows)
    enrich.maybe_enqueue(new_row)
    send_webhook('/webhook/toodizzle-events', {
        'event': 'task.created',
        'task': {'id': task_id, 'title': text, 'type': todo_type, 'date_added': date_added},
    })
    return redirect(url_for('index'))


@app.route('/delete/<item_id>', methods=['POST'])
def delete_todo(item_id):
    ensure_csv()
    with _csv_lock():
        rows = _read_rows_raw()
        new_rows = [r for r in rows if r['id'] != item_id]
        if len(new_rows) != len(rows):
            _atomic_write(new_rows)
    return ('', 204)


@app.route('/duplicate/<item_id>', methods=['POST'])
def duplicate_todo(item_id):
    ensure_csv()
    with _csv_lock():
        rows = _read_rows_raw()
        original = next((r for r in rows if r['id'] == item_id), None)
        if original:
            rows.append({
                'id': str(uuid.uuid4())[:8],
                'text': original.get('text', ''),
                'type': original.get('type', 'Other'),
                'subtype': original.get('subtype', ''),
                'date_added': datetime.now().strftime('%Y-%m-%d'),
                'done': '',
            })
            _atomic_write(rows)
    return ('', 204)


@app.route('/edit/<item_id>', methods=['POST'])
def edit_todo(item_id):
    text = request.form.get('text', '').strip()
    todo_type = request.form.get('type', 'Other').strip()
    if not text:
        return ('', 400)
    ensure_csv()
    with _csv_lock():
        rows = _read_rows_raw()
        new_subtype = request.form.get('subtype', '').strip() if todo_type == 'Tech' else ''
        for row in rows:
            if row['id'] == item_id:
                row['text'] = text
                row['type'] = todo_type
                row['subtype'] = new_subtype
                break
        _atomic_write(rows)
    return ('', 204)


@app.route('/done/<item_id>', methods=['POST'])
def toggle_done(item_id):
    ensure_csv()
    completed_task = None
    with _csv_lock():
        rows = _read_rows_raw()
        for row in rows:
            if row['id'] == item_id:
                if row['done'] == '':
                    row['done'] = 'yes'
                    completed_task = dict(row)
                else:
                    row['done'] = ''
                break
        _atomic_write(rows)
    if completed_task:
        send_webhook('/webhook/toodizzle-events', {
            'event': 'task.completed',
            'task': {'id': completed_task['id'], 'title': completed_task['text'], 'type': completed_task['type']},
        })
    return ('', 204)


# --- REST API ---

def _task_dict(row):
    return {col: row.get(col, '') for col in FIELDNAMES}


@app.route('/api/tasks', methods=['GET'])
def api_list_tasks():
    return jsonify([_task_dict(r) for r in read_todos()])


@app.route('/api/tasks/<item_id>', methods=['GET'])
def api_get_task(item_id):
    ensure_csv()
    with _csv_lock():
        rows = _read_rows_raw()
    row = next((r for r in rows if r['id'] == item_id), None)
    if row is None:
        return jsonify({'error': 'not found'}), 404
    return jsonify(_task_dict(row))


@app.route('/api/tasks', methods=['POST'])
def api_create_task():
    data = request.get_json(force=True, silent=True) or {}
    text = (data.get('text') or '').strip()
    if not text:
        return jsonify({'error': 'text is required'}), 400
    todo_type = (data.get('type') or 'Other').strip()
    new_row = {
        'id': str(uuid.uuid4())[:8],
        'text': text,
        'type': todo_type,
        'subtype': (data.get('subtype') or '').strip() if todo_type == 'Tech' else '',
        'date_added': datetime.now().strftime('%Y-%m-%d'),
        'done': '',
        'enriched': '',
    }
    ensure_csv()
    with _csv_lock():
        rows = _read_rows_raw()
        rows.append(new_row)
        _atomic_write(rows)
    enrich.maybe_enqueue(new_row)
    send_webhook('/webhook/toodizzle-events', {
        'event': 'task.created',
        'task': {'id': new_row['id'], 'title': new_row['text'], 'type': new_row['type'], 'date_added': new_row['date_added']},
    })
    return jsonify(new_row), 201


@app.route('/api/tasks/<item_id>', methods=['PUT'])
def api_update_task(item_id):
    data = request.get_json(force=True, silent=True) or {}
    ensure_csv()
    completed = None
    with _csv_lock():
        rows = _read_rows_raw()
        target = next((r for r in rows if r['id'] == item_id), None)
        if target is None:
            return jsonify({'error': 'not found'}), 404
        if 'text' in data:
            target['text'] = str(data['text']).strip()
        if 'type' in data:
            target['type'] = str(data['type']).strip()
        if 'subtype' in data:
            target['subtype'] = str(data['subtype']).strip() if target['type'] == 'Tech' else ''
        if 'done' in data:
            was_done = target['done']
            target['done'] = 'yes' if data['done'] else ''
            if data['done'] and was_done != 'yes':
                completed = dict(target)
        _atomic_write(rows)
    if completed:
        send_webhook('/webhook/toodizzle-events', {
            'event': 'task.completed',
            'task': {'id': completed['id'], 'title': completed['text'], 'type': completed['type']},
        })
    return jsonify(_task_dict(target))


@app.route('/api/tasks/type/<task_type>', methods=['GET'])
def api_get_tasks_by_type(task_type):
    rows = [_task_dict(r) for r in read_todos()
            if r.get('type', '').lower() == task_type.lower()]
    return jsonify(rows)


@app.route('/api/tasks/title/<path:query>', methods=['GET'])
def api_get_tasks_by_title(query):
    q = query.lower()
    rows = [_task_dict(r) for r in read_todos()
            if q in r.get('text', '').lower()]
    return jsonify(rows)


@app.route('/api/tasks/<item_id>', methods=['DELETE'])
def api_delete_task(item_id):
    ensure_csv()
    with _csv_lock():
        rows = _read_rows_raw()
        original_len = len(rows)
        rows = [r for r in rows if r['id'] != item_id]
        if len(rows) == original_len:
            return jsonify({'error': 'not found'}), 404
        _atomic_write(rows)
    return ('', 204)
