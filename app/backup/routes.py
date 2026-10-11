import json
import os
import re
from datetime import datetime, date, timedelta
from decimal import Decimal

from flask import (
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)
from flask_login import login_required, current_user
from sqlalchemy import MetaData, insert, inspect, text

from app.backup import backup_bp
from app.decorators import role_required
from app.models import BackupLog, db
from app.tenancy import is_super_admin, is_agency_admin

BACKUP_PREFIX = 'Mianbrothers'
AGENCY_BACKUP_PREFIX = 'Agency'
BACKUP_RETENTION_DAYS = 7
SAFE_FULL_BACKUP_NAME = re.compile(
    r'^Mianbrothers_\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}\.json$'
)
SAFE_AGENCY_BACKUP_NAME = re.compile(
    r'^Agency_(\d+)_\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}\.json$'
)

# Tables that may hold tenant rows (agency_id). agencies/users handled specially.
SKIP_TABLES = {'alembic_version', 'sqlite_sequence'}


def _backup_dir(agency_id=None):
    path = current_app.config.get('BACKUP_DIR')
    if not path:
        base = os.path.abspath(os.path.join(current_app.root_path, '..'))
        path = os.path.join(base, 'backups')
    if agency_id is not None:
        path = os.path.join(path, f'agency_{int(agency_id)}')
    os.makedirs(path, exist_ok=True)
    return path


def _is_sqlite():
    return str(db.engine.url).startswith('sqlite')


def _serialize_value(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat(sep=' ', timespec='seconds')
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, bytes):
        return value.decode('utf-8', errors='replace')
    if isinstance(value, bool):
        return value
    return value


def _quote_ident(name):
    if _is_sqlite():
        return f'"{name}"'
    return f'`{name}`'


def _table_columns(inspector, table_name):
    return {c['name'] for c in inspector.get_columns(table_name)}


def _app_tables():
    inspector = inspect(db.engine)
    return [t for t in inspector.get_table_names() if t not in SKIP_TABLES]


def _backup_mode():
    """'full' for super_admin, 'agency' for agency admin."""
    if is_super_admin():
        return 'full'
    if is_agency_admin() and current_user.agency_id:
        return 'agency'
    return None


def _parse_backup_meta(filename, mode):
    if mode == 'full':
        if not SAFE_FULL_BACKUP_NAME.match(filename):
            return None
        stamp = filename.replace(f'{BACKUP_PREFIX}_', '').replace('.json', '')
        try:
            created = datetime.strptime(stamp, '%Y-%m-%d_%H-%M-%S')
        except ValueError:
            return None
        return {'filename': filename, 'created': created, 'agency_id': None}

    m = SAFE_AGENCY_BACKUP_NAME.match(filename)
    if not m:
        return None
    agency_id = int(m.group(1))
    stamp = filename.replace(f'{AGENCY_BACKUP_PREFIX}_{agency_id}_', '').replace('.json', '')
    try:
        created = datetime.strptime(stamp, '%Y-%m-%d_%H-%M-%S')
    except ValueError:
        return None
    return {'filename': filename, 'created': created, 'agency_id': agency_id}


def _cleanup_old_backups(mode, agency_id=None):
    folder = _backup_dir(agency_id if mode == 'agency' else None)
    cutoff = datetime.now() - timedelta(days=BACKUP_RETENTION_DAYS)
    for name in os.listdir(folder):
        meta = _parse_backup_meta(name, mode)
        if meta is None:
            continue
        if mode == 'agency' and meta['agency_id'] != int(agency_id):
            continue
        if meta['created'] < cutoff:
            try:
                os.remove(os.path.join(folder, name))
            except OSError:
                pass


def _list_backups(mode, agency_id=None):
    folder = _backup_dir(agency_id if mode == 'agency' else None)
    items = []
    for name in os.listdir(folder):
        meta = _parse_backup_meta(name, mode)
        if meta is None:
            continue
        if mode == 'agency' and meta['agency_id'] != int(agency_id):
            continue
        path = os.path.join(folder, name)
        try:
            size = os.path.getsize(path)
        except OSError:
            continue
        prefix = BACKUP_PREFIX if mode == 'full' else f'{AGENCY_BACKUP_PREFIX} {agency_id}'
        items.append({
            'filename': name,
            'label': f'{prefix} {meta["created"].strftime("%Y-%m-%d %H:%M:%S")}',
            'created': meta['created'],
            'size_kb': round(size / 1024, 1),
            'scope': mode,
        })
    items.sort(key=lambda x: x['created'], reverse=True)
    return items


def _dump_query_rows(conn, sql, params=None):
    result = conn.execute(text(sql), params or {})
    columns = list(result.keys())
    rows = []
    for row in result:
        rows.append({col: _serialize_value(val) for col, val in zip(columns, row)})
    return columns, rows


def create_full_backup_file():
    """Dump all application tables into a dated JSON backup file."""
    tables = _app_tables()
    payload = {
        'scope': 'full',
        'created_at': datetime.now().isoformat(sep=' ', timespec='seconds'),
        'database': db.engine.url.database,
        'tables': {},
    }

    with db.engine.connect() as conn:
        for table_name in tables:
            columns, rows = _dump_query_rows(
                conn, f'SELECT * FROM {_quote_ident(table_name)}'
            )
            payload['tables'][table_name] = {'columns': columns, 'rows': rows}

    stamp = datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
    filename = f'{BACKUP_PREFIX}_{stamp}.json'
    path = os.path.join(_backup_dir(), filename)
    with open(path, 'w', encoding='utf-8') as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    return filename


def create_agency_backup_file(agency_id):
    """Dump only one agency's rows into JSON."""
    agency_id = int(agency_id)
    inspector = inspect(db.engine)
    tables = [t for t in inspector.get_table_names() if t not in SKIP_TABLES]

    payload = {
        'scope': 'agency',
        'agency_id': agency_id,
        'created_at': datetime.now().isoformat(sep=' ', timespec='seconds'),
        'database': db.engine.url.database,
        'tables': {},
    }

    with db.engine.connect() as conn:
        # Agency row
        if 'agencies' in tables:
            columns, rows = _dump_query_rows(
                conn,
                f'SELECT * FROM {_quote_ident("agencies")} WHERE id = :aid',
                {'aid': agency_id},
            )
            payload['tables']['agencies'] = {'columns': columns, 'rows': rows}

        for table_name in tables:
            if table_name == 'agencies':
                continue
            cols = _table_columns(inspector, table_name)
            if 'agency_id' not in cols:
                continue
            columns, rows = _dump_query_rows(
                conn,
                f'SELECT * FROM {_quote_ident(table_name)} WHERE agency_id = :aid',
                {'aid': agency_id},
            )
            payload['tables'][table_name] = {'columns': columns, 'rows': rows}

    stamp = datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
    filename = f'{AGENCY_BACKUP_PREFIX}_{agency_id}_{stamp}.json'
    path = os.path.join(_backup_dir(agency_id), filename)
    with open(path, 'w', encoding='utf-8') as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    return filename


def restore_full_from_payload(payload):
    """Replace entire database contents from a full-backup JSON payload."""
    if not isinstance(payload, dict):
        raise ValueError('Invalid backup file.')
    if payload.get('scope') == 'agency':
        raise ValueError('This is an agency backup. Use an agency account to restore it.')

    tables_data = payload.get('tables') or {}
    if not tables_data:
        raise ValueError('Backup file has no table data.')

    metadata = MetaData()
    metadata.reflect(bind=db.engine)
    sqlite = _is_sqlite()

    with db.engine.begin() as conn:
        if sqlite:
            conn.execute(text('PRAGMA foreign_keys = OFF'))
        else:
            conn.execute(text('SET FOREIGN_KEY_CHECKS = 0'))

        table_names = list(tables_data.keys())
        reflected = [t.name for t in reversed(metadata.sorted_tables)]
        clear_order = [t for t in reflected if t in tables_data] + [
            t for t in table_names if t not in reflected
        ]

        for table_name in clear_order:
            if table_name not in metadata.tables:
                continue
            conn.execute(text(f'DELETE FROM {_quote_ident(table_name)}'))

        _insert_tables(conn, metadata, tables_data)

        if sqlite:
            conn.execute(text('PRAGMA foreign_keys = ON'))
        else:
            conn.execute(text('SET FOREIGN_KEY_CHECKS = 1'))

    db.session.remove()


def restore_agency_from_payload(payload, agency_id):
    """Replace only one agency's data from an agency-scoped JSON payload."""
    agency_id = int(agency_id)
    if not isinstance(payload, dict):
        raise ValueError('Invalid backup file.')
    if payload.get('scope') != 'agency':
        raise ValueError('Not an agency backup file.')
    if int(payload.get('agency_id') or 0) != agency_id:
        raise ValueError('Backup belongs to a different agency.')

    tables_data = payload.get('tables') or {}
    if not tables_data:
        raise ValueError('Backup file has no table data.')

    # Force every restored row onto this agency (safety)
    for table_name, info in tables_data.items():
        if table_name == 'agencies':
            continue
        columns = info.get('columns') or []
        if 'agency_id' not in columns:
            continue
        for row in info.get('rows') or []:
            row['agency_id'] = agency_id

    metadata = MetaData()
    metadata.reflect(bind=db.engine)
    inspector = inspect(db.engine)
    sqlite = _is_sqlite()

    with db.engine.begin() as conn:
        if sqlite:
            conn.execute(text('PRAGMA foreign_keys = OFF'))
        else:
            conn.execute(text('SET FOREIGN_KEY_CHECKS = 0'))

        known = set(inspector.get_table_names())
        reflected = [t.name for t in reversed(metadata.sorted_tables)]
        for table_name in reflected:
            if table_name == 'agencies' or table_name not in known:
                continue
            cols = _table_columns(inspector, table_name)
            if 'agency_id' not in cols:
                continue
            conn.execute(
                text(f'DELETE FROM {_quote_ident(table_name)} WHERE agency_id = :aid'),
                {'aid': agency_id},
            )

        agency_info = tables_data.get('agencies') or {}
        agency_rows = agency_info.get('rows') or []
        if agency_rows and 'agencies' in metadata.tables:
            row = dict(agency_rows[0])
            row['id'] = agency_id
            table = metadata.tables['agencies']
            valid = [c for c in (agency_info.get('columns') or []) if c in table.c and c != 'id']
            if valid:
                sets = ', '.join(f'{_quote_ident(c)} = :{c}' for c in valid)
                params = {c: row.get(c) for c in valid}
                params['aid'] = agency_id
                conn.execute(
                    text(f'UPDATE {_quote_ident("agencies")} SET {sets} WHERE id = :aid'),
                    params,
                )

        insert_data = {k: v for k, v in tables_data.items() if k != 'agencies'}
        _insert_tables(conn, metadata, insert_data)

        if sqlite:
            conn.execute(text('PRAGMA foreign_keys = ON'))
        else:
            conn.execute(text('SET FOREIGN_KEY_CHECKS = 1'))

    db.session.remove()


def restore_full_backup_file(filename):
    """Replace entire database from a server-side full backup file."""
    if not SAFE_FULL_BACKUP_NAME.match(filename):
        raise ValueError('Invalid backup file name.')
    path = os.path.join(_backup_dir(), filename)
    if not os.path.isfile(path):
        raise FileNotFoundError('Backup file not found.')
    with open(path, 'r', encoding='utf-8') as fh:
        payload = json.load(fh)
    restore_full_from_payload(payload)


def restore_agency_backup_file(filename, agency_id):
    """Replace one agency from a server-side agency backup file."""
    agency_id = int(agency_id)
    meta = _parse_backup_meta(filename, 'agency')
    if meta is None or meta['agency_id'] != agency_id:
        raise ValueError('Invalid agency backup file for this agency.')
    path = os.path.join(_backup_dir(agency_id), filename)
    if not os.path.isfile(path):
        raise FileNotFoundError('Backup file not found.')
    with open(path, 'r', encoding='utf-8') as fh:
        payload = json.load(fh)
    restore_agency_from_payload(payload, agency_id)


def _log_backup_event(*, filename, scope, action, size_bytes=None, agency_id=None):
    entry = BackupLog(
        user_id=current_user.id,
        agency_id=agency_id,
        filename=filename,
        scope=scope,
        action=action,
        size_bytes=size_bytes,
        created_at=datetime.utcnow(),
    )
    db.session.add(entry)
    db.session.commit()
    return entry


def _user_backup_logs(search_q=''):
    """Logs for the current admin only (super_admin / agency admin)."""
    q = BackupLog.query.filter_by(user_id=current_user.id)
    if is_agency_admin() and current_user.agency_id and not is_super_admin():
        q = q.filter_by(agency_id=current_user.agency_id)
    if search_q:
        like = f'%{search_q}%'
        q = q.filter(BackupLog.filename.ilike(like))
    return q.order_by(BackupLog.created_at.desc()).limit(100).all()


def _insert_tables(conn, metadata, tables_data):
    table_names = list(tables_data.keys())
    insert_order = [t.name for t in metadata.sorted_tables if t.name in tables_data]
    insert_order += [t for t in table_names if t not in insert_order]

    for table_name in insert_order:
        info = tables_data[table_name]
        columns = info.get('columns') or []
        rows = info.get('rows') or []
        if not columns or not rows:
            continue
        if table_name not in metadata.tables:
            continue

        table = metadata.tables[table_name]
        valid_cols = [c for c in columns if c in table.c]
        if not valid_cols:
            continue

        stmt = insert(table)
        batch = []
        for row in rows:
            batch.append({col: row.get(col) for col in valid_cols})
            if len(batch) >= 200:
                conn.execute(stmt, batch)
                batch = []
        if batch:
            conn.execute(stmt, batch)


def _wants_file_response():
    return (
        request.args.get('download') == '1'
        or 'application/json' in (request.headers.get('Accept') or '')
        or request.headers.get('X-Requested-With') == 'OFBackup'
    )


def _send_backup_file(path, filename):
    response = send_file(
        path,
        mimetype='application/json',
        as_attachment=True,
        download_name=filename,
    )
    response.headers['X-Backup-Filename'] = filename
    response.headers['Cache-Control'] = 'no-store'
    return response


@backup_bp.route('/', methods=['GET'])
@login_required
@role_required('super_admin', 'admin')
def index():
    mode = _backup_mode()
    if not mode:
        flash('Backup is not available for your account.', 'danger')
        return redirect(url_for('dashboard.index'))

    agency_id = current_user.agency_id if mode == 'agency' else None
    _cleanup_old_backups(mode, agency_id)
    search_q = (request.args.get('search') or '').strip()
    logs = _user_backup_logs(search_q)
    last_create = next((L for L in logs if L.action == 'create'), None)

    # Attach whether server still has the file (optional re-download)
    enriched = []
    for entry in logs:
        if entry.scope == 'agency' and entry.agency_id:
            path = os.path.join(_backup_dir(entry.agency_id), entry.filename)
        else:
            path = os.path.join(_backup_dir(), entry.filename)
        enriched.append({
            'entry': entry,
            'has_file': os.path.isfile(path) if entry.action == 'create' else False,
            'size_kb': round((entry.size_bytes or 0) / 1024, 1) if entry.size_bytes else None,
        })

    return render_template(
        'backup/index.html',
        logs=enriched,
        last_backup=last_create,
        retention_days=BACKUP_RETENTION_DAYS,
        search=search_q,
        backup_mode=mode,
        agency_id=agency_id,
    )


@backup_bp.route('/create', methods=['POST'])
@login_required
@role_required('super_admin', 'admin')
def create():
    mode = _backup_mode()
    if not mode:
        flash('Backup is not available for your account.', 'danger')
        return redirect(url_for('dashboard.index'))

    try:
        if mode == 'full':
            _cleanup_old_backups('full')
            filename = create_full_backup_file()
            path = os.path.join(_backup_dir(), filename)
            agency_id = None
            scope_label = 'Full app'
        else:
            agency_id = current_user.agency_id
            _cleanup_old_backups('agency', agency_id)
            filename = create_agency_backup_file(agency_id)
            path = os.path.join(_backup_dir(agency_id), filename)
            scope_label = 'Agency'

        size_bytes = os.path.getsize(path) if os.path.isfile(path) else None
        _log_backup_event(
            filename=filename,
            scope=mode,
            action='create',
            size_bytes=size_bytes,
            agency_id=agency_id,
        )

        if _wants_file_response() and os.path.isfile(path):
            return _send_backup_file(path, filename)

        flash(f'{scope_label} backup created: {filename}', 'success')
    except Exception as exc:
        try:
            db.session.rollback()
        except Exception:
            pass
        if request.headers.get('X-Requested-With') == 'OFBackup':
            return jsonify(ok=False, error=str(exc)), 500
        flash(f'Backup failed: {exc}', 'danger')
    return redirect(url_for('backup.index'))


@backup_bp.route('/download/<path:filename>', methods=['GET'])
@login_required
@role_required('super_admin', 'admin')
def download(filename):
    mode = _backup_mode()
    if not mode:
        flash('Backup is not available for your account.', 'danger')
        return redirect(url_for('backup.index'))

    # Only allow re-download of files this user created
    owned = BackupLog.query.filter_by(
        user_id=current_user.id,
        filename=filename,
        action='create',
    ).first()
    if not owned:
        flash('Backup not found in your logs.', 'danger')
        return redirect(url_for('backup.index'))

    meta = _parse_backup_meta(filename, mode)
    if meta is None:
        flash('Invalid backup file.', 'danger')
        return redirect(url_for('backup.index'))

    if mode == 'agency':
        if meta['agency_id'] != int(current_user.agency_id):
            flash('Invalid backup file.', 'danger')
            return redirect(url_for('backup.index'))
        path = os.path.join(_backup_dir(current_user.agency_id), filename)
    else:
        path = os.path.join(_backup_dir(), filename)

    if not os.path.isfile(path):
        flash('Backup file no longer on server. Use your downloaded copy to restore.', 'warning')
        return redirect(url_for('backup.index'))
    return _send_backup_file(path, filename)


@backup_bp.route('/restore', methods=['POST'])
@login_required
@role_required('super_admin', 'admin')
def restore():
    mode = _backup_mode()
    if not mode:
        flash('Restore is not available for your account.', 'danger')
        return redirect(url_for('dashboard.index'))

    confirm = (request.form.get('confirm_restore') or '').strip().lower()
    if confirm != 'yes':
        flash('Restore cancelled. You must confirm before restoring.', 'warning')
        return redirect(url_for('backup.index'))

    upload = request.files.get('backup_upload')
    if not upload or not (upload.filename or '').strip():
        flash('Please choose a backup JSON file from your computer.', 'danger')
        return redirect(url_for('backup.index'))

    filename = os.path.basename(upload.filename).strip()
    if not filename.lower().endswith('.json'):
        flash('Backup must be a .json file.', 'danger')
        return redirect(url_for('backup.index'))

    try:
        raw = upload.read()
        payload = json.loads(raw.decode('utf-8'))
    except Exception:
        flash('Could not read backup file. Choose a valid OctaneFlow JSON backup.', 'danger')
        return redirect(url_for('backup.index'))

    try:
        if mode == 'full':
            restore_full_from_payload(payload)
            flash(
                f'Full data restored from {filename}. Previous live data has been replaced.',
                'success',
            )
            agency_id = None
        else:
            restore_agency_from_payload(payload, current_user.agency_id)
            flash(
                f'Agency data restored from {filename}. '
                f'Only your agency was replaced — other agencies were not changed.',
                'success',
            )
            agency_id = current_user.agency_id

        _log_backup_event(
            filename=filename,
            scope=mode,
            action='restore',
            size_bytes=len(raw) if raw is not None else None,
            agency_id=agency_id,
        )
    except Exception as exc:
        try:
            db.session.rollback()
        except Exception:
            pass
        flash(f'Restore failed: {exc}', 'danger')
    return redirect(url_for('backup.index'))
