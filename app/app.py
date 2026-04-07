import csv
import os
import re
import uuid
import threading
from datetime import datetime
from urllib.parse import urlparse

import markupsafe
import markdown as _md
from flask import Flask, render_template, request, redirect, url_for, jsonify

app = Flask(__name__)

CSV_PATH = '/data/todos.csv'
FIELDNAMES = ['id', 'text', 'type', 'subtype', 'date_added', 'done']
_lock = threading.Lock()
URL_RE = re.compile(r'https?://[^\s<>"]+')


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
    os.makedirs('/data', exist_ok=True)
    if not os.path.exists(CSV_PATH):
        with open(CSV_PATH, 'w', newline='') as f:
            csv.DictWriter(f, fieldnames=FIELDNAMES).writeheader()
        return
    with open(CSV_PATH, 'r', newline='') as f:
        existing = csv.DictReader(f).fieldnames or []
    if any(col not in existing for col in FIELDNAMES):
        with open(CSV_PATH, 'r', newline='') as f:
            rows = list(csv.DictReader(f))
        with open(CSV_PATH, 'w', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
            writer.writeheader()
            for row in rows:
                for col in FIELDNAMES:
                    row.setdefault(col, '')
                writer.writerow({col: row[col] for col in FIELDNAMES})


def read_todos():
    ensure_csv()
    with _lock:
        with open(CSV_PATH, 'r', newline='') as f:
            rows = list(csv.DictReader(f))
    for row in rows:
        for col in FIELDNAMES:
            row.setdefault(col, '')
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


@app.route('/submit', methods=['POST'])
def submit():
    text = request.form.get('text', '').strip()
    if not text:
        return redirect(url_for('index'))
    ensure_csv()
    with _lock:
        with open(CSV_PATH, 'a', newline='') as f:
            csv.DictWriter(f, fieldnames=FIELDNAMES).writerow({
                'id': str(uuid.uuid4())[:8],
                'text': text,
                'type': request.form.get('type', 'Other').strip(),
                'subtype': request.form.get('subtype', '').strip() if request.form.get('type') == 'Tech' else '',
                'date_added': datetime.now().strftime('%Y-%m-%d'),
                'done': '',
            })
    return redirect(url_for('index'))


@app.route('/delete/<item_id>', methods=['POST'])
def delete_todo(item_id):
    ensure_csv()
    with _lock:
        with open(CSV_PATH, 'r', newline='') as f:
            rows = list(csv.DictReader(f))
        rows = [r for r in rows if r['id'] != item_id]
        with open(CSV_PATH, 'w', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
            writer.writeheader()
            for row in rows:
                writer.writerow({col: row.get(col, '') for col in FIELDNAMES})
    return ('', 204)


@app.route('/duplicate/<item_id>', methods=['POST'])
def duplicate_todo(item_id):
    ensure_csv()
    with _lock:
        with open(CSV_PATH, 'r', newline='') as f:
            rows = list(csv.DictReader(f))
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
            with open(CSV_PATH, 'w', newline='') as f:
                writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
                writer.writeheader()
                for row in rows:
                    writer.writerow({col: row.get(col, '') for col in FIELDNAMES})
    return ('', 204)


@app.route('/edit/<item_id>', methods=['POST'])
def edit_todo(item_id):
    text = request.form.get('text', '').strip()
    todo_type = request.form.get('type', 'Other').strip()
    if not text:
        return ('', 400)
    ensure_csv()
    with _lock:
        with open(CSV_PATH, 'r', newline='') as f:
            rows = list(csv.DictReader(f))
        new_subtype = request.form.get('subtype', '').strip() if todo_type == 'Tech' else ''
        for row in rows:
            if row['id'] == item_id:
                row['text'] = text
                row['type'] = todo_type
                row['subtype'] = new_subtype
                break
        with open(CSV_PATH, 'w', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
            writer.writeheader()
            for row in rows:
                writer.writerow({col: row.get(col, '') for col in FIELDNAMES})
    return ('', 204)


@app.route('/done/<item_id>', methods=['POST'])
def toggle_done(item_id):
    ensure_csv()
    with _lock:
        with open(CSV_PATH, 'r', newline='') as f:
            rows = list(csv.DictReader(f))
        for row in rows:
            if row['id'] == item_id:
                row['done'] = '' if row['done'] == 'yes' else 'yes'
                break
        with open(CSV_PATH, 'w', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
            writer.writeheader()
            for row in rows:
                writer.writerow({col: row.get(col, '') for col in FIELDNAMES})
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
    with _lock:
        with open(CSV_PATH, 'r', newline='') as f:
            rows = list(csv.DictReader(f))
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
    }
    ensure_csv()
    with _lock:
        with open(CSV_PATH, 'a', newline='') as f:
            csv.DictWriter(f, fieldnames=FIELDNAMES).writerow(new_row)
    return jsonify(new_row), 201


@app.route('/api/tasks/<item_id>', methods=['PUT'])
def api_update_task(item_id):
    data = request.get_json(force=True, silent=True) or {}
    ensure_csv()
    with _lock:
        with open(CSV_PATH, 'r', newline='') as f:
            rows = list(csv.DictReader(f))
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
            target['done'] = 'yes' if data['done'] else ''
        with open(CSV_PATH, 'w', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
            writer.writeheader()
            for row in rows:
                writer.writerow({col: row.get(col, '') for col in FIELDNAMES})
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
    with _lock:
        with open(CSV_PATH, 'r', newline='') as f:
            rows = list(csv.DictReader(f))
        original_len = len(rows)
        rows = [r for r in rows if r['id'] != item_id]
        if len(rows) == original_len:
            return jsonify({'error': 'not found'}), 404
        with open(CSV_PATH, 'w', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
            writer.writeheader()
            for row in rows:
                writer.writerow({col: row.get(col, '') for col in FIELDNAMES})
    return ('', 204)
