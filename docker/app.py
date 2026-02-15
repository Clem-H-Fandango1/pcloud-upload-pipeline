from flask import Flask, render_template_string, jsonify, request, session, redirect, url_for
import json
import os
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path
import sqlite3
from functools import wraps

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', os.urandom(24).hex())

# =============================================================================
# Configuration
# =============================================================================

PIPELINE_BASE = os.environ.get('PIPELINE_BASE', '/data/pipeline')
TEMP_DIR = f'{PIPELINE_BASE}/.temp'
STAGING_DIR = f'{PIPELINE_BASE}/staging'
UPLOAD_DIR = f'{PIPELINE_BASE}/uploads'
FAILED_DIR = f'{PIPELINE_BASE}/failed'
DB_FILE = f'{PIPELINE_BASE}/transfers.db'
PROGRESS_FILE = f'{PIPELINE_BASE}/pcloud-progress.json'
SETTINGS_FILE = f'{PIPELINE_BASE}/settings.json'
STAGING_LOG = os.environ.get('STAGING_LOG', f'{PIPELINE_BASE}/logs/pcloud-staging.log')
UPLOAD_LOG = os.environ.get('UPLOAD_LOG', f'{PIPELINE_BASE}/logs/pcloud-upload.log')
ALLOWED_PATHS = os.environ.get('ALLOWED_PATHS', '/data').split(',')

DEFAULT_SETTINGS = {'max_concurrent_copies': 2}
DASHBOARD_PASSWORD = os.environ.get('DASHBOARD_PASSWORD', 'pcloud123')
APP_VERSION = 'v2.0'

# =============================================================================
# Settings
# =============================================================================

def load_settings():
    try:
        with open(SETTINGS_FILE, 'r') as f:
            return {**DEFAULT_SETTINGS, **json.load(f)}
    except Exception:
        return DEFAULT_SETTINGS.copy()

def save_settings(settings):
    with open(SETTINGS_FILE, 'w') as f:
        json.dump(settings, f)

copy_semaphore = threading.Semaphore(load_settings()['max_concurrent_copies'])

# =============================================================================
# Auth
# =============================================================================

def require_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('authenticated'):
            if request.path.startswith('/api/'):
                return jsonify({'error': 'Not authenticated'}), 401
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated

def is_path_allowed(path):
    abs_path = os.path.abspath(path)
    return any(abs_path.startswith(a) for a in ALLOWED_PATHS)

# =============================================================================
# Helpers
# =============================================================================

def format_size(size_bytes):
    if size_bytes <= 0:
        return "0 B"
    units = ['B', 'KB', 'MB', 'GB', 'TB']
    i = 0
    s = float(size_bytes)
    while s >= 1024 and i < len(units) - 1:
        s /= 1024
        i += 1
    return f"{s:.1f} {units[i]}"

def format_eta(seconds):
    if seconds <= 0:
        return ''
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s"
    h = seconds // 3600
    m = (seconds % 3600) // 60
    return f"{h}h {m}m"

# =============================================================================
# Copy Job Tracking (in-memory with cancel support)
# =============================================================================

copy_jobs_lock = threading.Lock()
copy_jobs = {}
_job_counter = 0

def new_job_id():
    global _job_counter
    _job_counter += 1
    return f"job_{_job_counter}_{int(time.time())}"

def copy_with_progress(src, dst, job_id, file_index):
    buf_size = 1024 * 1024  # 1MB chunks
    with open(src, 'rb') as fsrc:
        with open(dst, 'wb') as fdst:
            while True:
                # Check cancellation
                with copy_jobs_lock:
                    job = copy_jobs.get(job_id)
                    if not job or job.get('cancelled'):
                        raise InterruptedError('Cancelled')
                buf = fsrc.read(buf_size)
                if not buf:
                    break
                fdst.write(buf)
                with copy_jobs_lock:
                    if job_id in copy_jobs:
                        copy_jobs[job_id]['files'][file_index]['bytes_copied'] += len(buf)
    shutil.copystat(src, dst)

def copy_file_worker(source, operation, job_id):
    try:
        with copy_jobs_lock:
            job = copy_jobs.get(job_id)
            if not job:
                return

        for i, file_info in enumerate(job['files']):
            # Check cancellation before starting each file
            with copy_jobs_lock:
                if copy_jobs.get(job_id, {}).get('cancelled'):
                    break

            copy_semaphore.acquire()
            try:
                src_path = file_info['src_path']
                rel_path = file_info['rel_path']

                with copy_jobs_lock:
                    if copy_jobs.get(job_id, {}).get('cancelled'):
                        break
                    if job_id in copy_jobs:
                        copy_jobs[job_id]['files'][i]['status'] = 'copying'

                if job['is_dir']:
                    temp_path = os.path.join(TEMP_DIR, job['name'], rel_path + '.tmp')
                    final_path = os.path.join(STAGING_DIR, job['name'], rel_path)
                else:
                    temp_path = os.path.join(TEMP_DIR, rel_path + '.tmp')
                    final_path = os.path.join(STAGING_DIR, rel_path)

                os.makedirs(os.path.dirname(temp_path), exist_ok=True)

                # Handle name conflicts
                if os.path.exists(final_path):
                    base_dir = os.path.dirname(final_path)
                    basename = os.path.basename(final_path)
                    name, ext = os.path.splitext(basename)
                    counter = 1
                    while os.path.exists(final_path):
                        final_path = os.path.join(base_dir, f"{name}_{counter}{ext}")
                        counter += 1

                if operation == 'move':
                    shutil.move(src_path, temp_path)
                    with copy_jobs_lock:
                        if job_id in copy_jobs:
                            copy_jobs[job_id]['files'][i]['bytes_copied'] = file_info['size']
                else:
                    copy_with_progress(src_path, temp_path, job_id, i)

                # Create staging dir right before rename to avoid race
                # with staging watcher cleaning up empty parent dirs
                os.makedirs(os.path.dirname(final_path), exist_ok=True)
                os.rename(temp_path, final_path)

                with copy_jobs_lock:
                    if job_id in copy_jobs:
                        copy_jobs[job_id]['files'][i]['status'] = 'done'
                        copy_jobs[job_id]['files'][i]['bytes_copied'] = file_info['size']

            except InterruptedError:
                # Cancelled - clean up temp file
                with copy_jobs_lock:
                    if job_id in copy_jobs:
                        copy_jobs[job_id]['files'][i]['status'] = 'cancelled'
                try:
                    os.remove(temp_path)
                except Exception:
                    pass
                break
            except Exception as e:
                with copy_jobs_lock:
                    if job_id in copy_jobs:
                        copy_jobs[job_id]['files'][i]['status'] = 'failed'
                        copy_jobs[job_id]['files'][i]['error'] = str(e)
                        copy_jobs[job_id]['error'] = f"Failed: {file_info['rel_path']}: {e}"
                # Clean up temp file on failure
                try:
                    if 'temp_path' in dir():
                        os.remove(temp_path)
                except Exception:
                    pass
            finally:
                copy_semaphore.release()

        # Mark job finished (keep visible until manually dismissed)
        with copy_jobs_lock:
            if job_id in copy_jobs:
                job = copy_jobs[job_id]
                all_done = all(f['status'] in ('done', 'failed', 'cancelled') for f in job['files'])
                if all_done:
                    has_errors = any(f['status'] in ('failed', 'cancelled') for f in job['files'])
                    job['finished'] = True
                    job['finished_at'] = time.time()
                    job['has_errors'] = has_errors

        # Auto-remove successful jobs after 30s, keep failed ones longer
        time.sleep(30)
        with copy_jobs_lock:
            job = copy_jobs.get(job_id)
            if job and job.get('finished') and not job.get('has_errors'):
                copy_jobs.pop(job_id, None)

    except Exception as e:
        with copy_jobs_lock:
            if job_id in copy_jobs:
                copy_jobs[job_id]['error'] = str(e)
                copy_jobs[job_id]['finished'] = True
                copy_jobs[job_id]['finished_at'] = time.time()
                copy_jobs[job_id]['has_errors'] = True

# =============================================================================
# Pipeline Data Functions
# =============================================================================

last_valid_progress = {}

def get_current_upload_progress():
    global last_valid_progress
    try:
        with open(PROGRESS_FILE, 'r') as f:
            content = f.read().strip()
            if content:
                data = json.loads(content)
                if isinstance(data, dict):
                    last_valid_progress = data
                    return data
    except Exception:
        pass
    return last_valid_progress

def get_copying_data():
    with copy_jobs_lock:
        result = []
        for job_id, job in copy_jobs.items():
            files = []
            total_bytes = 0
            copied_bytes = 0
            for f in job['files']:
                total_bytes += f['size']
                copied_bytes += f['bytes_copied']
                files.append({
                    'name': f['rel_path'],
                    'size': format_size(f['size']),
                    'size_bytes': f['size'],
                    'bytes_copied': format_size(f['bytes_copied']),
                    'status': f['status'],
                    'percent': int(f['bytes_copied'] / f['size'] * 100) if f['size'] > 0 else 0,
                    'error': f.get('error')
                })
            overall_pct = int(copied_bytes / total_bytes * 100) if total_bytes > 0 else 0
            elapsed = time.time() - job['started_at']
            speed = copied_bytes / elapsed if elapsed > 1 else 0
            remaining_bytes = total_bytes - copied_bytes
            eta_seconds = int(remaining_bytes / speed) if speed > 0 else 0

            done_count = sum(1 for f in job['files'] if f['status'] == 'done')
            failed_count = sum(1 for f in job['files'] if f['status'] in ('failed', 'cancelled'))

            result.append({
                'job_id': job_id,
                'name': job['name'],
                'operation': job['operation'],
                'is_dir': job['is_dir'],
                'files': files,
                'total_size': format_size(total_bytes),
                'total_size_bytes': total_bytes,
                'copied_size': format_size(copied_bytes),
                'percent': overall_pct,
                'done_count': done_count,
                'failed_count': failed_count,
                'total_count': len(job['files']),
                'speed': format_size(int(speed)) + '/s' if speed > 0 else '',
                'eta': format_eta(eta_seconds) if eta_seconds > 0 else '',
                'error': job.get('error'),
                'finished': job.get('finished', False),
                'has_errors': job.get('has_errors', False),
                'cancelled': job.get('cancelled', False)
            })
        return result

def get_orphaned_temp_files():
    """Find .tmp files in .temp/ not tracked by any active copy job."""
    tracked_paths = set()
    with copy_jobs_lock:
        for job in copy_jobs.values():
            for f in job['files']:
                if job['is_dir']:
                    tp = os.path.join(TEMP_DIR, job['name'], f['rel_path'] + '.tmp')
                else:
                    tp = os.path.join(TEMP_DIR, f['rel_path'] + '.tmp')
                tracked_paths.add(tp)

    orphans = []
    if not os.path.exists(TEMP_DIR):
        return orphans

    for root, dirs, files in os.walk(TEMP_DIR):
        for fname in files:
            filepath = os.path.join(root, fname)
            if filepath not in tracked_paths:
                try:
                    size = os.path.getsize(filepath)
                    relpath = os.path.relpath(filepath, TEMP_DIR)
                    orphans.append({
                        'path': filepath,
                        'name': relpath,
                        'size': format_size(size),
                        'size_bytes': size
                    })
                except Exception:
                    continue
    return orphans

def _walk_files(base_dir):
    """Walk a directory and return list of (filepath, size_bytes) tuples."""
    result = []
    if not os.path.isdir(base_dir):
        return result
    for root, dirs, files in os.walk(base_dir):
        for fname in files:
            filepath = os.path.join(root, fname)
            try:
                size_bytes = os.path.getsize(filepath)
                result.append((filepath, size_bytes))
            except OSError:
                continue
    return result

def get_queued_data():
    result = []
    for filepath, size_bytes in _walk_files(STAGING_DIR):
        relpath = os.path.relpath(filepath, STAGING_DIR)
        result.append({
            'name': relpath,
            'path': filepath,
            'size': format_size(size_bytes),
            'size_bytes': size_bytes
        })
    return result

def get_uploading_data():
    progress_data = get_current_upload_progress()
    result = []
    for filepath, size_bytes in _walk_files(UPLOAD_DIR):
        relpath = os.path.relpath(filepath, UPLOAD_DIR)
        percent = progress_data.get(relpath, 0)
        result.append({
            'name': relpath,
            'path': filepath,
            'size': format_size(size_bytes),
            'size_bytes': size_bytes,
            'uploading': percent > 0,
            'percent': percent
        })
    return result

def get_failed_data():
    result = []
    for filepath, size_bytes in _walk_files(FAILED_DIR):
        relpath = os.path.relpath(filepath, FAILED_DIR)
        result.append({
            'name': relpath,
            'path': filepath,
            'size': format_size(size_bytes),
            'size_bytes': size_bytes
        })
    return result

def get_recent_data():
    try:
        conn = sqlite3.connect(DB_FILE, timeout=5)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT source_path, state, size_bytes, progress, error, updated_at "
            "FROM transfers ORDER BY updated_at DESC LIMIT 50"
        ).fetchall()
        conn.close()
        result = []
        for row in rows:
            size_bytes = row['size_bytes'] or 0
            result.append({
                'name': os.path.basename(row['source_path']),
                'full_path': row['source_path'],
                'state': row['state'],
                'size': format_size(size_bytes),
                'size_bytes': size_bytes,
                'progress': row['progress'] or 0,
                'error': row['error'],
                'updated_at': row['updated_at']
            })
        return result
    except Exception:
        return []

# =============================================================================
# Upload Speed & ETA
# =============================================================================

_speed_lock = threading.Lock()
_speed_samples = []

def calculate_upload_speed():
    progress = get_current_upload_progress()
    upload_files = get_uploading_data()
    size_map = {f['name']: f['size_bytes'] for f in upload_files}
    total_uploaded = 0
    for name, pct in progress.items():
        if name in size_map:
            total_uploaded += int(size_map[name] * pct / 100)
    now = time.time()
    with _speed_lock:
        _speed_samples.append((now, total_uploaded))
        cutoff = now - 30
        while _speed_samples and _speed_samples[0][0] < cutoff:
            _speed_samples.pop(0)
        if len(_speed_samples) >= 2:
            dt = _speed_samples[-1][0] - _speed_samples[0][0]
            db = _speed_samples[-1][1] - _speed_samples[0][1]
            if dt > 0 and db > 0:
                return db / dt
    return 0

def calculate_pipeline_eta(copying, queued, uploading, upload_speed):
    remaining = 0
    for job in copying:
        remaining += job.get('total_size_bytes', 0) * (100 - job.get('percent', 0)) / 100
    for f in queued:
        remaining += f.get('size_bytes', 0)
    for f in uploading:
        size = f.get('size_bytes', 0)
        pct = f.get('percent', 0)
        remaining += int(size * (100 - pct) / 100)
    if upload_speed > 0 and remaining > 0:
        return int(remaining / upload_speed)
    return 0

# =============================================================================
# Activity Log
# =============================================================================

def _read_log_tail(filepath, marker, max_lines=30):
    """Read last N lines containing marker from a log file using pure Python."""
    matches = []
    try:
        with open(filepath, 'r', errors='replace') as f:
            for line in f:
                if marker in line:
                    matches.append(line.rstrip())
            return matches[-max_lines:]
    except (OSError, IOError):
        return []

def get_activity_log_data():
    staging_lines = _read_log_tail(STAGING_LOG, '[STAGE]', 30)
    upload_lines = _read_log_tail(UPLOAD_LOG, '[UPLOAD]', 30)
    entries = []
    for line in staging_lines:
        try:
            parts = line.split(' ', 2)
            if len(parts) < 3:
                continue
            t = parts[0]
            rest = parts[2]
            if "Queued:" in rest:
                entries.append({'time': t, 'type': 'queued', 'label': 'QUEUED', 'text': rest.split("Queued:")[1].strip()})
            elif "Waiting" in rest:
                entries.append({'time': t, 'type': 'waiting', 'label': 'WAITING', 'text': 'Checking file stability'})
            elif "STAGE" in rest and "Skipping" not in rest:
                entries.append({'time': t, 'type': 'detected', 'label': 'DETECTED', 'text': rest.replace("[STAGE]", "").strip()})
        except Exception:
            continue
    for line in upload_lines:
        try:
            parts = line.split(' ', 2)
            if len(parts) < 3:
                continue
            t = parts[0]
            rest = parts[2]
            if "Uploading:" in rest:
                text = rest.replace("[UPLOAD]", "").replace("Uploading:", "").strip()
                entries.append({'time': t, 'type': 'uploading', 'label': 'UPLOADING', 'text': text})
            elif "Verified:" in rest:
                entries.append({'time': t, 'type': 'verified', 'label': 'VERIFIED', 'text': rest.split(":")[-1].strip()})
            elif "FAILED" in rest:
                entries.append({'time': t, 'type': 'failed', 'label': 'FAILED', 'text': rest.split(":")[-1].strip()})
        except Exception:
            continue
    entries = sorted(entries, key=lambda x: x['time'])[-30:]
    return entries

# =============================================================================
# Startup Cleanup
# =============================================================================

def init_db():
    """Enable WAL mode for concurrent read/write access."""
    try:
        conn = sqlite3.connect(DB_FILE, timeout=5)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.close()
        print("SQLite WAL mode enabled")
    except Exception as e:
        print(f"DB init error: {e}")

def cleanup_stale_db():
    """Mark stale uploading entries from previous runs."""
    try:
        conn = sqlite3.connect(DB_FILE, timeout=5)
        count = conn.execute("SELECT COUNT(*) FROM transfers WHERE state='uploading'").fetchone()[0]
        if count > 0:
            conn.execute(
                "UPDATE transfers SET state='interrupted', error='Dashboard restarted', "
                "updated_at=datetime('now') WHERE state='uploading'"
            )
            conn.commit()
            print(f"Cleaned up {count} stale 'uploading' DB entries")
        conn.close()
    except Exception as e:
        print(f"DB cleanup error: {e}")

init_db()
cleanup_stale_db()

# =============================================================================
# API Routes
# =============================================================================

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        if request.form.get('password') == DASHBOARD_PASSWORD:
            session['authenticated'] = True
            return redirect(url_for('index'))
        return render_template_string(LOGIN_TEMPLATE, error=True)
    return render_template_string(LOGIN_TEMPLATE, error=False)

@app.route('/logout')
def logout():
    session.pop('authenticated', None)
    return redirect(url_for('login'))

@app.route('/api/data')
@require_auth
def api_data():
    copying = get_copying_data()
    orphans = get_orphaned_temp_files()
    queued = get_queued_data()
    uploading = get_uploading_data()
    recent = get_recent_data()
    failed = get_failed_data()
    upload_speed = calculate_upload_speed()
    pipeline_eta = calculate_pipeline_eta(copying, queued, uploading, upload_speed)
    fail_count = sum(1 for r in recent if r['state'] == 'failed')

    # Summary stats
    total_queued_bytes = sum(f['size_bytes'] for f in queued)
    total_uploading_bytes = sum(f['size_bytes'] for f in uploading)
    total_orphan_bytes = sum(o['size_bytes'] for o in orphans)

    return jsonify({
        'settings': load_settings(),
        'copying': copying,
        'orphans': orphans,
        'queued': queued,
        'upload_files': uploading,
        'recent': recent,
        'failed': failed,
        'fail_count': fail_count,
        'upload_speed': format_size(int(upload_speed)) + '/s' if upload_speed > 0 else '',
        'pipeline_eta': format_eta(pipeline_eta),
        'activity_log': get_activity_log_data(),
        'stats': {
            'queued_total': format_size(total_queued_bytes),
            'uploading_total': format_size(total_uploading_bytes),
            'orphan_total': format_size(total_orphan_bytes),
            'orphan_count': len(orphans),
        },
        'timestamp': datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        'version': APP_VERSION
    })

@app.route('/api/browse')
@require_auth
def browse():
    path = request.args.get('path', '/zfs1')
    if not is_path_allowed(path):
        return jsonify({'error': 'Access denied'}), 403
    if not os.path.exists(path):
        return jsonify({'error': 'Path not found'}), 404
    try:
        items = []
        for entry in sorted(os.scandir(path), key=lambda e: (not e.is_dir(), e.name.lower())):
            try:
                stat = entry.stat()
                items.append({
                    'name': entry.name,
                    'path': entry.path,
                    'is_dir': entry.is_dir(),
                    'size': stat.st_size if entry.is_file() else 0,
                    'modified': datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M")
                })
            except Exception:
                continue
        parent = str(Path(path).parent) if path != '/' else None
        if parent and not is_path_allowed(parent):
            parent = None
        return jsonify({'current_path': path, 'parent': parent, 'items': items})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/transfer', methods=['POST'])
@require_auth
def transfer():
    data = request.json
    paths = data.get('paths', [])
    operation = data.get('operation', 'copy')

    os.makedirs(TEMP_DIR, exist_ok=True)
    os.makedirs(STAGING_DIR, exist_ok=True)

    if not paths:
        return jsonify({'error': 'No paths provided'}), 400

    queued = 0
    errors = []
    for path in paths:
        if not is_path_allowed(path) or not os.path.exists(path):
            errors.append({'path': path, 'message': 'Access denied or not found'})
            continue
        try:
            name = os.path.basename(path)
            is_dir = os.path.isdir(path)
            job_id = new_job_id()

            file_list = []
            if is_dir:
                for f in Path(path).rglob('*'):
                    if f.is_file():
                        rel = str(f.relative_to(path))
                        file_list.append({
                            'src_path': str(f), 'rel_path': rel, 'name': f.name,
                            'size': f.stat().st_size, 'bytes_copied': 0,
                            'status': 'pending', 'error': None
                        })
            else:
                file_list.append({
                    'src_path': path, 'rel_path': name, 'name': name,
                    'size': os.path.getsize(path), 'bytes_copied': 0,
                    'status': 'pending', 'error': None
                })

            if not file_list:
                errors.append({'path': path, 'message': 'No files found'})
                continue

            with copy_jobs_lock:
                copy_jobs[job_id] = {
                    'name': name, 'is_dir': is_dir, 'operation': operation,
                    'files': file_list, 'started_at': time.time(),
                    'error': None, 'finished': False, 'has_errors': False,
                    'cancelled': False
                }

            t = threading.Thread(target=copy_file_worker, args=(path, operation, job_id), daemon=True)
            t.start()
            queued += 1
        except Exception as e:
            errors.append({'path': path, 'message': str(e)})

    return jsonify({'queued': queued, 'errors': errors})

@app.route('/api/cancel-job', methods=['POST'])
@require_auth
def cancel_job():
    job_id = request.json.get('job_id')
    if not job_id:
        return jsonify({'error': 'No job_id'}), 400
    with copy_jobs_lock:
        job = copy_jobs.get(job_id)
        if not job:
            return jsonify({'error': 'Job not found'}), 404
        job['cancelled'] = True
    return jsonify({'status': 'ok'})

@app.route('/api/dismiss-job', methods=['POST'])
@require_auth
def dismiss_job():
    job_id = request.json.get('job_id')
    if not job_id:
        return jsonify({'error': 'No job_id'}), 400
    with copy_jobs_lock:
        copy_jobs.pop(job_id, None)
    return jsonify({'status': 'ok'})

@app.route('/api/clear-orphans', methods=['POST'])
@require_auth
def clear_orphans():
    orphans = get_orphaned_temp_files()
    count = 0
    for orphan in orphans:
        try:
            os.remove(orphan['path'])
            count += 1
        except Exception:
            pass
    # Clean empty dirs
    if os.path.exists(TEMP_DIR):
        for root, dirs, files in os.walk(TEMP_DIR, topdown=False):
            for d in dirs:
                try:
                    os.rmdir(os.path.join(root, d))
                except Exception:
                    pass
    return jsonify({'cleared': count})

@app.route('/api/delete-item', methods=['POST'])
@require_auth
def delete_item():
    data = request.json
    filepath = data.get('path')
    if not filepath:
        return jsonify({'error': 'No path'}), 400
    # Only allow deleting from pipeline dirs
    allowed_prefixes = [STAGING_DIR, FAILED_DIR, UPLOAD_DIR]
    abs_path = os.path.abspath(filepath)
    if not any(abs_path.startswith(p) for p in allowed_prefixes):
        return jsonify({'error': 'Access denied'}), 403
    try:
        if os.path.isfile(abs_path):
            os.remove(abs_path)
        elif os.path.isdir(abs_path):
            shutil.rmtree(abs_path)
        # Clean empty parent dirs
        parent = os.path.dirname(abs_path)
        for prefix in allowed_prefixes:
            if abs_path.startswith(prefix):
                while parent != prefix and os.path.isdir(parent):
                    try:
                        os.rmdir(parent)
                        parent = os.path.dirname(parent)
                    except OSError:
                        break
                break
        return jsonify({'status': 'ok'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/retry-failed', methods=['POST'])
@require_auth
def retry_failed():
    try:
        count = 0
        for root, dirs, files in os.walk(FAILED_DIR):
            for fname in files:
                src = os.path.join(root, fname)
                rel = os.path.relpath(src, FAILED_DIR)
                dst = os.path.join(UPLOAD_DIR, rel)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.move(src, dst)
                count += 1
        for root, dirs, files in os.walk(FAILED_DIR, topdown=False):
            for d in dirs:
                try:
                    os.rmdir(os.path.join(root, d))
                except Exception:
                    pass
        return jsonify({'retried': count})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/clear-staging', methods=['POST'])
@require_auth
def clear_staging():
    try:
        for entry in os.scandir(STAGING_DIR):
            if entry.is_dir():
                shutil.rmtree(entry.path)
            else:
                os.remove(entry.path)
        return jsonify({'status': 'ok'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/clear-recent', methods=['POST'])
@require_auth
def clear_recent():
    try:
        conn = sqlite3.connect(DB_FILE, timeout=5)
        conn.execute("DELETE FROM transfers WHERE state IN ('completed', 'failed', 'interrupted')")
        conn.commit()
        conn.close()
        return jsonify({'status': 'ok'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/settings', methods=['GET', 'POST'])
@require_auth
def settings():
    global copy_semaphore
    if request.method == 'POST':
        data = request.json
        current = load_settings()
        if 'max_concurrent_copies' in data:
            val = max(1, min(int(data['max_concurrent_copies']), 8))
            current['max_concurrent_copies'] = val
            copy_semaphore = threading.Semaphore(val)
        save_settings(current)
        return jsonify(current)
    return jsonify(load_settings())

# =============================================================================
# Templates
# =============================================================================

LOGIN_TEMPLATE = """<!DOCTYPE html>
<html><head>
<title>Login - pCloud Monitor</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Inter,sans-serif;background:#060a10;color:#c9d1d9;display:flex;justify-content:center;align-items:center;height:100vh}
.login{background:rgba(14,18,25,0.9);border:1px solid rgba(48,54,61,0.6);border-radius:20px;padding:48px;width:100%;max-width:400px;box-shadow:0 24px 80px rgba(0,0,0,0.6),0 0 1px rgba(88,166,255,0.2),0 0 60px rgba(88,166,255,0.03)}
h1{color:#e6edf3;font-size:22px;font-weight:700;letter-spacing:-0.5px}
.sub{color:#484f58;font-size:13px;margin:6px 0 32px}
label{display:block;color:#6e7681;font-size:11px;text-transform:uppercase;letter-spacing:1px;margin-bottom:8px;font-weight:600}
input[type=password]{width:100%;padding:14px 16px;background:rgba(13,17,23,0.8);border:1px solid #21262d;border-radius:10px;color:#e6edf3;font-size:14px;transition:all .2s}
input[type=password]:focus{outline:none;border-color:#388bfd;box-shadow:0 0 0 3px rgba(56,139,253,0.15)}
button{width:100%;padding:14px;background:linear-gradient(135deg,#238636,#2ea043);color:#fff;border:none;border-radius:10px;font-size:14px;font-weight:600;cursor:pointer;margin-top:20px;transition:all .2s}
button:hover{transform:translateY(-1px);box-shadow:0 6px 20px rgba(35,134,54,0.4)}
.err{background:rgba(248,81,73,0.08);border:1px solid rgba(248,81,73,0.2);color:#f85149;padding:12px;border-radius:8px;margin-bottom:16px;font-size:13px}
.glow{position:absolute;top:-50%;left:-50%;width:200%;height:200%;background:radial-gradient(circle at 50% 50%,rgba(88,166,255,0.03) 0%,transparent 50%);pointer-events:none}
.wrap{position:relative;overflow:hidden}
</style></head>
<body><div class="login wrap"><div class="glow"></div>
<h1>pCloud Upload Monitor</h1><div class="sub">Enter password to continue</div>
{% if error %}<div class="err">Incorrect password</div>{% endif %}
<form method="post"><label>Password</label>
<input type="password" name="password" autofocus required>
<button type="submit">Sign In</button></form></div></body></html>"""

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html><head>
<title>pCloud Upload Monitor</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Inter,sans-serif;background:#060a10;color:#c9d1d9;min-height:100vh}

/* Scrollbar */
::-webkit-scrollbar{width:5px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:rgba(110,118,129,0.3);border-radius:3px}
::-webkit-scrollbar-thumb:hover{background:rgba(110,118,129,0.5)}

/* Header */
.top-bar{background:linear-gradient(180deg,rgba(14,18,25,0.98),rgba(10,14,20,0.95));border-bottom:1px solid;border-image:linear-gradient(90deg,transparent,rgba(88,166,255,0.2),rgba(163,113,247,0.2),transparent) 1;padding:14px 24px;position:sticky;top:0;z-index:100;backdrop-filter:blur(20px);display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap}
.top-left{display:flex;align-items:center;gap:14px}
.top-bar h1{font-size:16px;font-weight:700;color:#e6edf3;letter-spacing:-0.3px}
.ver{background:linear-gradient(135deg,rgba(88,166,255,0.15),rgba(163,113,247,0.15));color:#a5b4c6;padding:2px 10px;border-radius:10px;font-size:10px;font-weight:700;letter-spacing:0.5px}
.top-right{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.stat-pill{display:flex;align-items:center;gap:5px;padding:4px 12px;border-radius:8px;font-size:11px;font-weight:600;font-family:'SF Mono',SFMono-Regular,Consolas,monospace}
.stat-speed{background:rgba(63,185,80,0.1);color:#3fb950}
.stat-eta{background:rgba(163,113,247,0.1);color:#a371f7}
.stat-fail{background:rgba(248,81,73,0.1);color:#f85149;cursor:pointer}
.stat-orphan{background:rgba(240,136,62,0.1);color:#f0883e;cursor:pointer}
.hidden{display:none!important}
.logout{background:none;color:#484f58;border:1px solid #21262d;padding:5px 12px;border-radius:6px;font-size:11px;cursor:pointer;text-decoration:none;transition:all .15s}
.logout:hover{color:#8b949e;border-color:#30363d}

/* Layout */
.container{max-width:1800px;margin:0 auto;padding:0 20px 40px}
.section-hdr{display:flex;justify-content:space-between;align-items:center;margin:20px 0 10px;gap:8px}
.section-title{font-size:12px;font-weight:700;color:#484f58;text-transform:uppercase;letter-spacing:1.5px}
.toggle-btn{background:rgba(22,27,34,0.6);color:#6e7681;border:1px solid #21262d;padding:5px 14px;border-radius:6px;font-size:11px;cursor:pointer;transition:all .15s}
.toggle-btn:hover{color:#c9d1d9;border-color:#30363d;background:rgba(33,38,45,0.8)}

/* Buttons */
.btn{padding:6px 14px;border-radius:8px;font-size:11px;font-weight:600;cursor:pointer;transition:all .15s;border:none;display:inline-flex;align-items:center;gap:4px}
.btn:disabled{opacity:0.3;cursor:not-allowed}
.btn-green{background:linear-gradient(135deg,#238636,#2ea043);color:#fff}
.btn-green:hover:not(:disabled){box-shadow:0 2px 12px rgba(35,134,54,0.4);transform:translateY(-1px)}
.btn-purple{background:linear-gradient(135deg,#8957e5,#a371f7);color:#fff}
.btn-purple:hover:not(:disabled){box-shadow:0 2px 12px rgba(137,87,229,0.4);transform:translateY(-1px)}
.btn-ghost{background:rgba(22,27,34,0.6);color:#6e7681;border:1px solid #21262d}
.btn-ghost:hover:not(:disabled){color:#c9d1d9;border-color:#30363d}
.btn-danger{background:rgba(218,54,51,0.1);color:#f85149;border:1px solid rgba(218,54,51,0.2)}
.btn-danger:hover:not(:disabled){background:rgba(218,54,51,0.2);border-color:rgba(218,54,51,0.4)}
.btn-sm{padding:3px 8px;font-size:10px;border-radius:5px}
.btn-icon{width:22px;height:22px;padding:0;display:inline-flex;align-items:center;justify-content:center;border-radius:5px;background:transparent;color:#484f58;border:none;cursor:pointer;font-size:12px;transition:all .15s}
.btn-icon:hover{color:#f85149;background:rgba(248,81,73,0.1)}
.btn-icon.retry:hover{color:#3fb950;background:rgba(63,185,80,0.1)}

/* File Browser */
.browser{background:rgba(14,18,25,0.6);border:1px solid rgba(48,54,61,0.4);border-radius:14px;padding:16px;margin-bottom:16px;display:none}
.browser.visible{display:block}
.loc-bar{display:flex;gap:6px;margin-bottom:10px;align-items:center;flex-wrap:wrap}
.breadcrumb{flex:1;background:rgba(6,10,16,0.6);padding:8px 14px;border-radius:8px;font-family:'SF Mono',SFMono-Regular,Consolas,monospace;font-size:12px;color:#484f58;border:1px solid rgba(33,38,45,0.6);min-width:200px}
.browser-actions{display:flex;gap:8px;align-items:center;padding:8px 0;border-bottom:1px solid rgba(33,38,45,0.6);margin-bottom:8px}
.sel-info{color:#484f58;font-size:11px;margin-left:auto;font-family:'SF Mono',SFMono-Regular,Consolas,monospace}
.file-list{max-height:500px;overflow-y:auto;padding-right:2px}
.fi{display:flex;align-items:center;padding:8px 10px;margin:2px 0;border-radius:6px;transition:all .12s;border:1px solid transparent;gap:10px;font-size:13px}
.fi:hover{background:rgba(22,27,34,0.7);border-color:rgba(33,38,45,0.5)}
.fi.selected{background:rgba(31,111,235,0.1);border-color:rgba(31,111,235,0.2)}
.fi-cb{width:15px;height:15px;cursor:pointer;accent-color:#388bfd;flex-shrink:0}
.fi-icon{width:20px;flex-shrink:0;text-align:center;font-size:14px}
.fi.folder .fi-icon,.fi.folder .fi-name{cursor:pointer}
.fi.folder .fi-icon:hover,.fi.folder .fi-name:hover{color:#58a6ff}
.fi-name{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-width:0}
.fi-size{color:#484f58;font-size:11px;font-family:'SF Mono',SFMono-Regular,Consolas,monospace;flex-shrink:0;min-width:70px;text-align:right}
.fi-date{color:#30363d;font-size:11px;flex-shrink:0;min-width:100px}

/* Pipeline */
.pipe-toolbar{display:flex;align-items:center;gap:10px;margin-bottom:10px;flex-wrap:wrap}
.pipe-toolbar label{color:#484f58;font-size:11px;font-weight:600}
.pipe-toolbar select{background:#0d1117;color:#c9d1d9;border:1px solid #21262d;border-radius:6px;padding:4px 8px;font-size:11px}
.pipe-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}
@media(max-width:1100px){.pipe-grid{grid-template-columns:repeat(2,1fr)}}
@media(max-width:600px){.pipe-grid{grid-template-columns:1fr}.top-bar{padding:10px 14px}}
.pipe-col{background:rgba(14,18,25,0.6);border:1px solid rgba(33,38,45,0.5);border-radius:14px;overflow:hidden;display:flex;flex-direction:column;min-height:120px}
.pipe-hdr{padding:10px 14px;font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:1.2px;display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid rgba(33,38,45,0.4)}
.pipe-hdr .cnt{background:rgba(255,255,255,0.06);padding:1px 7px;border-radius:8px;font-size:10px}
.pipe-hdr .sz{font-weight:400;color:inherit;opacity:0.6;font-size:9px;margin-left:6px}
.col-copy .pipe-hdr{background:linear-gradient(135deg,rgba(31,111,235,0.12),transparent);color:#58a6ff;border-bottom-color:rgba(31,111,235,0.15)}
.col-queue .pipe-hdr{background:linear-gradient(135deg,rgba(240,136,62,0.12),transparent);color:#f0883e;border-bottom-color:rgba(240,136,62,0.15)}
.col-upload .pipe-hdr{background:linear-gradient(135deg,rgba(137,87,229,0.12),transparent);color:#a371f7;border-bottom-color:rgba(137,87,229,0.15)}
.col-recent .pipe-hdr{background:linear-gradient(135deg,rgba(63,185,80,0.12),transparent);color:#3fb950;border-bottom-color:rgba(63,185,80,0.15)}
.pipe-body{padding:6px;flex:1;overflow-y:auto;max-height:500px}
.pi{padding:10px 12px;margin:3px 0;background:rgba(6,10,16,0.5);border-radius:8px;font-size:12px;border-left:3px solid #21262d;transition:all .15s;position:relative}
.pi:hover{background:rgba(13,17,23,0.8)}
.pi:hover .pi-actions{opacity:1}
.col-copy .pi{border-left-color:rgba(88,166,255,0.4)}
.col-queue .pi{border-left-color:rgba(240,136,62,0.4)}
.col-upload .pi{border-left-color:rgba(163,113,247,0.4)}
.col-recent .pi.completed{border-left-color:rgba(63,185,80,0.4)}
.col-recent .pi.failed,.col-recent .pi.interrupted{border-left-color:rgba(248,81,73,0.4)}
.pi-name{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;margin-bottom:3px;font-weight:500;padding-right:24px}
.pi-meta{color:#484f58;font-size:10px;display:flex;justify-content:space-between;align-items:center;font-family:'SF Mono',SFMono-Regular,Consolas,monospace}
.pi-actions{position:absolute;top:8px;right:8px;opacity:0;transition:opacity .15s;display:flex;gap:2px}
.pi-state{font-weight:600;font-size:10px}
.pi-state.completed{color:#3fb950}
.pi-state.failed,.pi-state.interrupted{color:#f85149}
.pi-state.uploading{color:#a371f7}
.pi-state.copying{color:#58a6ff}

/* Progress bar */
.pbar{width:100%;height:3px;border-radius:2px;overflow:hidden;margin-top:5px;background:rgba(255,255,255,0.04)}
.pbar-fill{height:100%;border-radius:2px;transition:width .8s ease}
.pbar-blue .pbar-fill{background:linear-gradient(90deg,#1f6feb,#58a6ff)}
.pbar-purple .pbar-fill{background:linear-gradient(90deg,#8957e5,#d2a8ff)}
.pbar-green .pbar-fill{background:linear-gradient(90deg,#238636,#3fb950)}

/* Copy job sub-files */
.cj-files{margin-top:6px;padding-top:6px;border-top:1px solid rgba(33,38,45,0.4);display:none}
.cj-files.expanded{display:block}
.cj-toggle{color:#484f58;font-size:10px;cursor:pointer;user-select:none;display:inline-flex;align-items:center;gap:3px}
.cj-toggle:hover{color:#8b949e}
.cj-sub{display:flex;align-items:center;gap:6px;padding:2px 0;font-size:10px;color:#6e7681}
.cj-sub .ico{width:12px;text-align:center;flex-shrink:0}
.cj-sub .ico.done{color:#3fb950}
.cj-sub .ico.copying{color:#58a6ff}
.cj-sub .ico.pending{color:#30363d}
.cj-sub .ico.failed,.cj-sub .ico.cancelled{color:#f85149}
.cj-sub .sname{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-width:0}
.cj-sub .spct{font-weight:600;min-width:30px;text-align:right;font-family:'SF Mono',SFMono-Regular,Consolas,monospace}
.cj-sub .spct.done{color:#3fb950}
.cj-sub .spct.copying{color:#58a6ff}

/* Orphan banner */
.orphan-banner{background:rgba(240,136,62,0.08);border:1px solid rgba(240,136,62,0.2);border-radius:10px;padding:12px 16px;margin-bottom:12px;display:flex;align-items:center;justify-content:space-between;gap:12px}
.orphan-banner .ob-text{font-size:12px;color:#f0883e}
.orphan-banner .ob-text strong{font-weight:700}

@keyframes pulse{0%,100%{opacity:1}50%{opacity:0.6}}
.pi.active{animation:pulse 2.5s ease-in-out infinite}
.empty{color:#21262d;font-size:11px;font-style:italic;text-align:center;padding:20px 8px}

/* Activity Log */
.log-section{background:rgba(14,18,25,0.6);border:1px solid rgba(33,38,45,0.4);border-radius:14px;padding:16px;max-height:400px;overflow-y:auto;display:none}
.log-section.visible{display:block}
.log-entry{padding:8px 10px;margin:3px 0;background:rgba(6,10,16,0.4);border-radius:6px;border-left:3px solid #21262d;font-size:12px;line-height:1.5}
.log-detected{border-left-color:#58a6ff}
.log-waiting{border-left-color:#f0883e}
.log-queued{border-left-color:#3fb950}
.log-uploading{border-left-color:#a371f7}
.log-verified{border-left-color:#3fb950;background:rgba(63,185,80,0.03)}
.log-failed{border-left-color:#f85149;background:rgba(248,81,73,0.03)}
.log-time{color:#30363d;font-family:'SF Mono',SFMono-Regular,Consolas,monospace;font-size:10px;margin-right:10px}
.log-label{font-weight:700;text-transform:uppercase;font-size:9px;padding:1px 5px;border-radius:3px;margin-right:8px;letter-spacing:0.5px}
.log-detected .log-label{color:#58a6ff;background:rgba(88,166,255,0.08)}
.log-waiting .log-label{color:#f0883e;background:rgba(240,136,62,0.08)}
.log-queued .log-label{color:#3fb950;background:rgba(63,185,80,0.08)}
.log-uploading .log-label{color:#a371f7;background:rgba(163,113,247,0.08)}
.log-verified .log-label{color:#3fb950;background:rgba(63,185,80,0.12)}
.log-failed .log-label{color:#f85149;background:rgba(248,81,73,0.12)}

/* Notifications */
.notif-container{position:fixed;top:60px;right:20px;z-index:1000;display:flex;flex-direction:column;gap:8px;pointer-events:none}
.notif{padding:12px 18px;border-radius:10px;color:#fff;font-size:12px;font-weight:500;max-width:380px;pointer-events:auto;box-shadow:0 8px 30px rgba(0,0,0,0.5);animation:notifIn .3s ease;position:relative;overflow:hidden}
.notif.success{background:linear-gradient(135deg,rgba(35,134,54,0.95),rgba(46,160,67,0.95))}
.notif.error{background:linear-gradient(135deg,rgba(190,44,40,0.95),rgba(218,54,51,0.95))}
.notif-bar{position:absolute;bottom:0;left:0;height:2px;background:rgba(255,255,255,0.3);transition:width linear}
@keyframes notifIn{from{transform:translateX(100px);opacity:0}to{transform:translateX(0);opacity:1}}

.refresh-info{text-align:right;color:#21262d;font-size:10px;margin-top:20px;font-family:'SF Mono',SFMono-Regular,Consolas,monospace}
</style></head>
<body>
<div class="top-bar">
 <div class="top-left">
  <h1>pCloud Upload Monitor</h1>
  <span class="ver" id="app-ver"></span>
 </div>
 <div class="top-right">
  <span class="stat-pill stat-speed hidden" id="pill-speed"></span>
  <span class="stat-pill stat-eta hidden" id="pill-eta"></span>
  <span class="stat-pill stat-fail hidden" id="pill-fail"></span>
  <span class="stat-pill stat-orphan hidden" id="pill-orphan"></span>
  <a href="/logout" class="logout">Logout</a>
 </div>
</div>

<div class="container">
 <div class="section-hdr">
  <span class="section-title">File Browser</span>
  <button class="toggle-btn" id="btn-toggle-browser">Show / Hide</button>
 </div>
 <div class="browser" id="file-browser">
  <div class="loc-bar">
   <button class="btn btn-ghost btn-sm" data-browse="/zfs1">zfs1</button>
   <button class="btn btn-ghost btn-sm" data-browse="/zfs2">zfs2</button>
   <button class="btn btn-ghost btn-sm" data-browse="/zfs3">zfs3</button>
  </div>
  <div class="browser-actions">
   <div class="breadcrumb" id="cur-path">/zfs1</div>
   <button class="btn btn-ghost btn-sm" id="btn-sel-all">Select All</button>
   <button class="btn btn-ghost btn-sm" id="btn-sel-clear">Clear</button>
   <span class="sel-info" id="sel-info">0 selected</span>
   <button class="btn btn-green" id="btn-copy" disabled>Upload (Copy)</button>
   <button class="btn btn-purple" id="btn-move" disabled>Upload (Move)</button>
  </div>
  <div class="file-list" id="file-list"><div class="empty">Click a location to browse</div></div>
 </div>

 <div class="section-hdr">
  <span class="section-title">Upload Pipeline</span>
 </div>
 <div id="orphan-banner" class="orphan-banner hidden">
  <span class="ob-text" id="orphan-text"></span>
  <button class="btn btn-danger btn-sm" id="btn-clear-orphans">Clear Orphans</button>
 </div>
 <div class="pipe-toolbar">
  <label>Parallel copies:</label>
  <select id="max-copies"></select>
  <button class="btn btn-ghost btn-sm" id="btn-clear-queue">Clear Queue</button>
  <button class="btn btn-ghost btn-sm" id="btn-retry-failed">Retry Failed</button>
  <button class="btn btn-ghost btn-sm" id="btn-clear-history">Clear History</button>
 </div>
 <div class="pipe-grid">
  <div class="pipe-col col-copy">
   <div class="pipe-hdr">Copying <span class="cnt" id="cnt-copy">0</span></div>
   <div class="pipe-body" id="col-copying"><div class="empty">No active copies</div></div>
  </div>
  <div class="pipe-col col-queue">
   <div class="pipe-hdr">Queued <span class="cnt" id="cnt-queue">0</span><span class="sz" id="sz-queue"></span></div>
   <div class="pipe-body" id="col-queued"><div class="empty">Queue empty</div></div>
  </div>
  <div class="pipe-col col-upload">
   <div class="pipe-hdr">Uploading <span class="cnt" id="cnt-upload">0</span><span class="sz" id="sz-upload"></span></div>
   <div class="pipe-body" id="col-uploading"><div class="empty">No active uploads</div></div>
  </div>
  <div class="pipe-col col-recent">
   <div class="pipe-hdr">Recent <span class="cnt" id="cnt-recent">0</span></div>
   <div class="pipe-body" id="col-recent"><div class="empty">No recent transfers</div></div>
  </div>
 </div>

 <div class="section-hdr">
  <span class="section-title">Activity Log</span>
  <button class="toggle-btn" id="btn-toggle-log">Show / Hide</button>
 </div>
 <div class="log-section" id="activity-log"><div class="empty">Loading...</div></div>
 <div class="refresh-info">Last updated: <span id="timestamp">--</span></div>
</div>

<div class="notif-container" id="notif-container"></div>

<script>
// === State ===
var curPath='/zfs1', selected=new Set(), allItems=[], lastSelIdx=-1, lastFailCnt=0;
var copyExpanded={};

// === Utils ===
function esc(s){var d=document.createElement('div');d.textContent=s;return d.innerHTML}
function fmtSz(b){if(!b)return'';var u=['B','KB','MB','GB','TB'],i=Math.floor(Math.log(b)/Math.log(1024));return(b/Math.pow(1024,i)).toFixed(1)+' '+u[i]}

// === Notifications ===
function notify(msg,type){
 var c=document.getElementById('notif-container');
 var n=document.createElement('div');
 n.className='notif '+(type||'success');
 n.textContent=msg;
 var bar=document.createElement('div');
 bar.className='notif-bar';
 bar.style.width='100%';
 n.appendChild(bar);
 c.appendChild(n);
 requestAnimationFrame(function(){bar.style.width='0%';bar.style.transitionDuration='4s'});
 setTimeout(function(){n.style.opacity='0';n.style.transition='opacity .3s';setTimeout(function(){n.remove()},300)},4000);
}

// === API helpers ===
function api(url,opts){
 return fetch(url,opts).then(function(r){
  if(r.status===401){window.location.href='/login';throw new Error('Auth')}
  return r.json()
 })
}
function apiPost(url,body){
 return api(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body||{})})
}

// === File Browser ===
function browseTo(path){
 curPath=path;selected.clear();lastSelIdx=-1;
 api('/api/browse?path='+encodeURIComponent(path)).then(function(d){
  if(d.error){notify(d.error,'error');return}
  document.getElementById('cur-path').textContent=d.current_path;
  allItems=d.items;
  var fl=document.getElementById('file-list'),h='';
  if(d.parent){
   h+='<div class="fi folder" data-path="'+esc(d.parent)+'">'
    +'<input type="checkbox" class="fi-cb" style="visibility:hidden">'
    +'<div class="fi-icon folder-nav">&#128193;</div>'
    +'<div class="fi-name folder-nav">..</div>'
    +'<div class="fi-size"></div><div class="fi-date"></div></div>';
  }
  d.items.forEach(function(it,idx){
   var cls=it.is_dir?'fi folder':'fi';
   var ico=it.is_dir?'&#128193;':'&#128196;';
   var sz=it.is_dir?'':fmtSz(it.size);
   var nav=it.is_dir?' folder-nav':'';
   h+='<div class="'+cls+'" data-path="'+esc(it.path)+'" data-idx="'+idx+'">'
    +'<input type="checkbox" class="fi-cb" data-idx="'+idx+'">'
    +'<div class="fi-icon'+nav+'">'+ico+'</div>'
    +'<div class="fi-name'+nav+'">'+esc(it.name)+'</div>'
    +'<div class="fi-size">'+sz+'</div>'
    +'<div class="fi-date">'+it.modified+'</div></div>';
  });
  fl.innerHTML=h||'<div class="empty">Empty directory</div>';
  updSel();
 }).catch(function(e){notify('Browse error','error')});
}

function updSel(){
 document.querySelectorAll('.fi').forEach(function(el){
  var p=el.dataset.path,cb=el.querySelector('.fi-cb');
  if(selected.has(p)){el.classList.add('selected');if(cb)cb.checked=true}
  else{el.classList.remove('selected');if(cb)cb.checked=false}
 });
 var n=selected.size;
 document.getElementById('sel-info').textContent=n+' selected';
 document.getElementById('btn-copy').disabled=!n;
 document.getElementById('btn-move').disabled=!n;
}

function doTransfer(op){
 if(!selected.size)return;
 var paths=Array.from(selected);
 var btn=document.getElementById(op==='copy'?'btn-copy':'btn-move');
 var orig=btn.textContent;btn.textContent='Working...';btn.disabled=true;
 apiPost('/api/transfer',{paths:paths,operation:op}).then(function(d){
  if(d.queued>0)notify('Queued '+d.queued+' item(s) for upload');
  if(d.errors)d.errors.forEach(function(e){notify('Error: '+e.message,'error')});
  selected.clear();btn.textContent=orig;updSel();browseTo(curPath);
 }).catch(function(e){notify('Transfer failed','error');btn.textContent=orig;updSel()});
}

// === Pipeline Rendering ===
function renderCopying(data){
 var el=document.getElementById('col-copying');
 var total=0;
 data.copying.forEach(function(j){total+=j.total_count});
 document.getElementById('cnt-copy').textContent=total;
 if(!data.copying.length){el.innerHTML='<div class="empty">No active copies</div>';return}
 el.innerHTML=data.copying.map(function(j){
  var fin=j.finished,err=j.has_errors,canc=j.cancelled;
  var cls='pi'+(fin?'':' active');
  var acts='<div class="pi-actions">';
  if(!fin&&!canc)acts+='<button class="btn-icon" data-cancel="'+j.job_id+'" title="Cancel">&#10005;</button>';
  if(fin)acts+='<button class="btn-icon" data-dismiss="'+j.job_id+'" title="Dismiss">&#10005;</button>';
  acts+='</div>';
  var etaT=j.eta?' &middot; '+j.eta:'';
  var spdT=j.speed?' &middot; '+j.speed:'';
  var stateLabel=canc?'Cancelled':fin?(err?'Errors':'Done'):j.percent+'%'+spdT+etaT;
  var stateClass=canc?'failed':fin?(err?'failed':'completed'):'copying';
  var h='<div class="'+cls+'">'
   +'<div class="pi-name">'+esc(j.name)+(j.is_dir?' ('+j.operation+')':'')+'</div>'
   +'<div class="pi-meta"><span>'+j.copied_size+' / '+j.total_size+'</span>'
   +'<span class="pi-state '+stateClass+'">'+stateLabel+'</span></div>'
   +'<div class="pbar pbar-blue"><div class="pbar-fill" style="width:'+j.percent+'%"></div></div>'
   +acts;
  if(j.is_dir&&j.files&&j.files.length>1){
   var isExp=copyExpanded[j.job_id];
   h+='<div class="cj-toggle" data-toggle-job="'+j.job_id+'">'+(isExp?'&#9660;':'&#9654;')+' '+j.done_count+'/'+j.total_count+' files'+(j.failed_count?' ('+j.failed_count+' failed)':'')+'</div>';
   h+='<div class="cj-files'+(isExp?' expanded':'')+'" id="cjf-'+j.job_id+'">';
   var shown=0,maxShow=20;
   for(var i=0;i<j.files.length&&shown<maxShow;i++){
    var f=j.files[i];
    if(f.status==='done'&&j.files.length>maxShow)continue;
    var si=f.status==='done'?'&#10003;':f.status==='copying'?'&#9654;':f.status==='failed'||f.status==='cancelled'?'&#10007;':'&#8226;';
    var pt=f.status==='copying'?f.percent+'%':f.status==='done'?'&#10003;':'';
    var pc=f.status==='done'?'done':f.status==='copying'?'copying':'';
    h+='<div class="cj-sub"><span class="ico '+f.status+'">'+si+'</span>'
     +'<span class="sname">'+esc(f.name)+'</span>'
     +'<span class="spct '+pc+'">'+pt+'</span></div>';
    shown++;
   }
   var rem=j.total_count-j.done_count-shown;
   if(rem>0)h+='<div class="cj-sub"><span class="ico pending">&#8230;</span><span class="sname" style="color:#30363d">+'+rem+' more</span><span class="spct"></span></div>';
   h+='</div>';
  }
  if(j.error&&!canc)h+='<div class="pi-meta" style="margin-top:4px"><span style="color:#f85149;font-size:10px">'+esc(j.error)+'</span></div>';
  h+='</div>';
  return h;
 }).join('');
}

function renderQueued(data){
 var el=document.getElementById('col-queued');
 document.getElementById('cnt-queue').textContent=data.queued.length;
 document.getElementById('sz-queue').textContent=data.stats.queued_total!=='0 B'?' ('+data.stats.queued_total+')':'';
 if(!data.queued.length){el.innerHTML='<div class="empty">Queue empty</div>';return}
 el.innerHTML=data.queued.map(function(f){
  var fname=f.name.split('/').pop();
  return'<div class="pi"><div class="pi-name" title="'+esc(f.name)+'">'+esc(fname)+'</div>'
   +'<div class="pi-meta"><span>'+f.size+'</span></div>'
   +'<div class="pi-actions"><button class="btn-icon" data-del="'+esc(f.path)+'" title="Remove">&#10005;</button></div></div>';
 }).join('');
}

function renderUploading(data){
 var el=document.getElementById('col-uploading');
 document.getElementById('cnt-upload').textContent=data.upload_files.length;
 document.getElementById('sz-upload').textContent=data.stats.uploading_total!=='0 B'?' ('+data.stats.uploading_total+')':'';
 if(!data.upload_files.length){el.innerHTML='<div class="empty">No active uploads</div>';return}
 el.innerHTML=data.upload_files.map(function(f){
  var pct=f.percent||0;
  var fname=f.name.split('/').pop();
  var bar=pct>0?'<div class="pbar pbar-purple"><div class="pbar-fill" style="width:'+pct+'%"></div></div>':'';
  return'<div class="pi active"><div class="pi-name" title="'+esc(f.name)+'">'+esc(fname)+'</div>'
   +'<div class="pi-meta"><span>'+f.size+'</span>'
   +'<span class="pi-state uploading">'+(pct>0?pct+'%':'Starting...')+'</span></div>'
   +bar+'</div>';
 }).join('');
}

function renderRecent(data){
 var el=document.getElementById('col-recent');
 document.getElementById('cnt-recent').textContent=data.recent.length;
 if(!data.recent.length){el.innerHTML='<div class="empty">No recent transfers</div>';return}
 el.innerHTML=data.recent.map(function(f){
  var ico=f.state==='completed'?'&#10003; ':f.state==='failed'||f.state==='interrupted'?'&#10007; ':'&#8635; ';
  var tm=f.updated_at?(f.updated_at.split(' ')[1]||''):'';
  var err=f.error?'<div class="pi-meta" style="margin-top:2px"><span style="color:#f85149;font-size:9px">'+esc(f.error)+'</span></div>':'';
  return'<div class="pi '+f.state+'"><div class="pi-name">'+ico+esc(f.name)+'</div>'
   +'<div class="pi-meta"><span>'+f.size+'</span><span class="pi-state '+f.state+'">'+f.state+'</span></div>'
   +(tm?'<div class="pi-meta"><span>'+tm+'</span></div>':'')
   +err+'</div>';
 }).join('');
}

// === Main update loop ===
function update(){
 api('/api/data').then(function(d){
  if(!d)return;
  document.getElementById('timestamp').textContent=d.timestamp;
  document.getElementById('app-ver').textContent=d.version||'';

  // Settings
  var sel=document.getElementById('max-copies');
  if(sel&&sel!==document.activeElement){
   var cur=d.settings.max_concurrent_copies;
   if(!sel.options.length){for(var i=1;i<=8;i++){var o=document.createElement('option');o.value=i;o.textContent=i;sel.appendChild(o)}}
   sel.value=cur;
  }

  // Top bar pills
  var ps=document.getElementById('pill-speed');
  if(d.upload_speed){ps.textContent='\u25B2 '+d.upload_speed;ps.classList.remove('hidden')}
  else ps.classList.add('hidden');

  var pe=document.getElementById('pill-eta');
  if(d.pipeline_eta){pe.textContent='ETA '+d.pipeline_eta;pe.classList.remove('hidden')}
  else pe.classList.add('hidden');

  var pf=document.getElementById('pill-fail');
  if(d.fail_count>0){pf.textContent=d.fail_count+' failed';pf.classList.remove('hidden')}
  else pf.classList.add('hidden');

  var po=document.getElementById('pill-orphan');
  if(d.stats.orphan_count>0){po.textContent=d.stats.orphan_count+' orphaned ('+d.stats.orphan_total+')';po.classList.remove('hidden')}
  else po.classList.add('hidden');

  // Orphan banner
  var ob=document.getElementById('orphan-banner');
  if(d.orphans&&d.orphans.length>0){
   document.getElementById('orphan-text').innerHTML='<strong>'+d.orphans.length+'</strong> orphaned temp file(s) ('+d.stats.orphan_total+') from interrupted copies';
   ob.classList.remove('hidden');
  }else ob.classList.add('hidden');

  // Failure notifications
  if(d.fail_count>lastFailCnt&&lastFailCnt>=0){
   notify((d.fail_count-lastFailCnt)+' upload(s) failed','error');
  }
  lastFailCnt=d.fail_count;

  // Render columns
  renderCopying(d);
  renderQueued(d);
  renderUploading(d);
  renderRecent(d);

  // Activity log
  var al=document.getElementById('activity-log');
  if(!d.activity_log.length)al.innerHTML='<div class="empty">No recent activity</div>';
  else al.innerHTML=d.activity_log.map(function(e){
   return'<div class="log-entry log-'+e.type+'"><span class="log-time">'+e.time+'</span>'
    +'<span class="log-label">'+e.label+'</span>'+esc(e.text)+'</div>';
  }).join('');
 }).catch(function(e){console.error('Update error:',e)});
}

// === Event Delegation ===
document.addEventListener('click',function(e){
 var t=e.target;

 // File browser: folder navigation
 var nav=t.closest('.folder-nav');
 if(nav){
  var fi=nav.closest('.fi');
  if(fi&&fi.classList.contains('folder')){browseTo(fi.dataset.path);return}
 }

 // File browser: checkbox
 var cb=t.closest('.fi-cb');
 if(cb){
  var fi=cb.closest('.fi');
  var path=fi.dataset.path;
  var idx=parseInt(cb.dataset.idx);
  if(e.shiftKey&&lastSelIdx!==-1){
   var s=Math.min(lastSelIdx,idx),en=Math.max(lastSelIdx,idx);
   for(var i=s;i<=en;i++)selected.add(allItems[i].path);
   updSel();
  }else{
   if(cb.checked)selected.add(path);else selected.delete(path);
   updSel();
  }
  lastSelIdx=idx;
  return;
 }

 // Location shortcuts
 var brw=t.closest('[data-browse]');
 if(brw){browseTo(brw.dataset.browse);return}

 // Cancel copy job
 var canc=t.closest('[data-cancel]');
 if(canc){
  apiPost('/api/cancel-job',{job_id:canc.dataset.cancel}).then(function(){notify('Job cancelled')});
  return;
 }

 // Dismiss copy job
 var dis=t.closest('[data-dismiss]');
 if(dis){
  apiPost('/api/dismiss-job',{job_id:dis.dataset.dismiss});
  return;
 }

 // Delete pipeline item
 var del=t.closest('[data-del]');
 if(del){
  apiPost('/api/delete-item',{path:del.dataset.del}).then(function(d){
   if(d.error)notify(d.error,'error');
  });
  return;
 }

 // Toggle copy job files
 var tog=t.closest('[data-toggle-job]');
 if(tog){
  var jid=tog.dataset.toggleJob;
  copyExpanded[jid]=!copyExpanded[jid];
  var cf=document.getElementById('cjf-'+jid);
  if(cf)cf.classList.toggle('expanded');
  tog.innerHTML=(copyExpanded[jid]?'&#9660;':'&#9654;')+' '+tog.textContent.substring(2);
  return;
 }
});

// Top-level button handlers
document.getElementById('btn-toggle-browser').addEventListener('click',function(){
 var b=document.getElementById('file-browser');
 b.classList.toggle('visible');
 if(b.classList.contains('visible'))browseTo(curPath);
});
document.getElementById('btn-toggle-log').addEventListener('click',function(){
 document.getElementById('activity-log').classList.toggle('visible');
});
document.getElementById('btn-sel-all').addEventListener('click',function(){
 allItems.forEach(function(it){selected.add(it.path)});updSel();
});
document.getElementById('btn-sel-clear').addEventListener('click',function(){
 selected.clear();updSel();
});
document.getElementById('btn-copy').addEventListener('click',function(){doTransfer('copy')});
document.getElementById('btn-move').addEventListener('click',function(){doTransfer('move')});
document.getElementById('max-copies').addEventListener('change',function(){
 apiPost('/api/settings',{max_concurrent_copies:parseInt(this.value)}).then(function(){
  notify('Parallel copies updated');
 }).catch(function(){notify('Failed to update','error')});
});
document.getElementById('btn-clear-queue').addEventListener('click',function(){
 if(!confirm('Clear all queued files?'))return;
 apiPost('/api/clear-staging').then(function(d){d.error?notify(d.error,'error'):notify('Queue cleared')});
});
document.getElementById('btn-retry-failed').addEventListener('click',function(){
 apiPost('/api/retry-failed').then(function(d){
  if(d.error)notify(d.error,'error');
  else if(d.retried>0)notify('Retrying '+d.retried+' file(s)');
  else notify('No failed files');
 });
});
document.getElementById('btn-clear-history').addEventListener('click',function(){
 apiPost('/api/clear-recent').then(function(d){d.error?notify(d.error,'error'):notify('History cleared')});
});
document.getElementById('btn-clear-orphans').addEventListener('click',function(){
 apiPost('/api/clear-orphans').then(function(d){
  if(d.error)notify(d.error,'error');
  else notify('Cleared '+d.cleared+' orphaned file(s)');
 });
});

// Start
update();
setInterval(update,2000);
</script>
</body></html>"""

@app.route("/")
@require_auth
def index():
    return render_template_string(HTML_TEMPLATE)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001)
