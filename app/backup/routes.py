import json
import os
import re
from datetime import datetime, date, timedelta
from decimal import Decimal

from flask import (
    current_app,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import login_required
from sqlalchemy import MetaData, insert, inspect, text

from app.backup import backup_bp
from app.decorators import role_required
from app.models import db

BACKUP_PREFIX = 'Mianbrothers'
BACKUP_RETENTION_DAYS = 7
SAFE_BACKUP_NAME = re.compile(r'^Mianbrothers_\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}\.json$')


def _backup_dir():
    path = current_app.config.get('BACKUP_DIR')
    if not path:
        base = os.path.abspath(os.path.join(current_app.root_path, '..'))
        path = os.path.join(base, 'backups')
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


def _backup_datetime_from_name(filename):
    """Parse Mianbrothers_YYYY-MM-DD_HH-MM-SS.json into a datetime."""
    if not SAFE_BACKUP_NAME.match(filename):
        return None
    stamp = filename.replace(f'{BACKUP_PREFIX}_', '').replace('.json', '')
    try:
        return datetime.strptime(stamp, '%Y-%m-%d_%H-%M-%S')
    except ValueError:
        return None


def _cleanup_old_backups():
    """Delete backup files older than BACKUP_RETENTION_DAYS."""
    folder = _backup_dir()
    cutoff = datetime.now() - timedelta(days=BACKUP_RETENTION_DAYS)
    removed = 0
    for name in os.listdir(folder):
        created = _backup_datetime_from_name(name)
        if created is None:
            continue
        if created < cutoff:
            path = os.path.join(folder, name)
            try:
                os.remove(path)
                removed += 1
            except OSError:
                pass
    return removed


def _list_backups():
    folder = _backup_dir()
    items = []
    for name in os.listdir(folder):
        created = _backup_datetime_from_name(name)
        if created is None:
            continue
        path = os.path.join(folder, name)
        try:
            size = os.path.getsize(path)
        except OSError:
            continue
        label = f'{BACKUP_PREFIX} {created.strftime("%Y-%m-%d %H:%M:%S")}'
        items.append({
            'filename': name,
            'label': label,
            'created': created,
            'size_kb': round(size / 1024, 1),
        })
    items.sort(key=lambda x: x['created'], reverse=True)
    return items


def _app_tables():
    """Return table names managed by this app (exclude alembic/system)."""
    inspector = inspect(db.engine)
    skip = {'alembic_version', 'sqlite_sequence'}
    return [t for t in inspector.get_table_names() if t not in skip]


def _quote_ident(name):
    if _is_sqlite():
        return f'"{name}"'
    return f'`{name}`'


def create_backup_file():
    """Dump all application tables into a dated JSON backup file."""
    tables = _app_tables()
    payload = {
        'created_at': datetime.now().isoformat(sep=' ', timespec='seconds'),
        'database': db.engine.url.database,
        'tables': {},
    }

    with db.engine.connect() as conn:
        for table_name in tables:
            result = conn.execute(text(f'SELECT * FROM {_quote_ident(table_name)}'))
            columns = list(result.keys())
            rows = []
            for row in result:
                rows.append({
                    col: _serialize_value(val)
                    for col, val in zip(columns, row)
                })
            payload['tables'][table_name] = {
                'columns': columns,
                'rows': rows,
            }

    stamp = datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
    filename = f'{BACKUP_PREFIX}_{stamp}.json'
    path = os.path.join(_backup_dir(), filename)
    with open(path, 'w', encoding='utf-8') as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    return filename


def restore_backup_file(filename):
    """Replace current database contents with the selected backup."""
    if not SAFE_BACKUP_NAME.match(filename):
        raise ValueError('Invalid backup file name.')

    path = os.path.join(_backup_dir(), filename)
    if not os.path.isfile(path):
        raise FileNotFoundError('Backup file not found.')

    with open(path, 'r', encoding='utf-8') as fh:
        payload = json.load(fh)

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

        if sqlite:
            conn.execute(text('PRAGMA foreign_keys = ON'))
        else:
            conn.execute(text('SET FOREIGN_KEY_CHECKS = 1'))

    db.session.remove()


@backup_bp.route('/', methods=['GET'])
@login_required
@role_required('admin')
def index():
    _cleanup_old_backups()
    backups = _list_backups()
    return render_template(
        'backup/index.html',
        backups=backups,
        retention_days=BACKUP_RETENTION_DAYS,
    )


@backup_bp.route('/create', methods=['POST'])
@login_required
@role_required('admin')
def create():
    try:
        _cleanup_old_backups()
        filename = create_backup_file()
        flash(f'Backup created successfully: {filename}', 'success')
    except Exception as exc:
        flash(f'Backup failed: {exc}', 'danger')
    return redirect(url_for('backup.index'))


@backup_bp.route('/restore', methods=['POST'])
@login_required
@role_required('admin')
def restore():
    filename = (request.form.get('backup_file') or '').strip()
    confirm = (request.form.get('confirm_restore') or '').strip().lower()

    if not filename:
        flash('Please select a backup to restore.', 'danger')
        return redirect(url_for('backup.index'))

    if confirm != 'yes':
        flash('Restore cancelled. You must confirm before restoring.', 'warning')
        return redirect(url_for('backup.index'))

    try:
        restore_backup_file(filename)
        flash(
            f'Full data restored from {filename}. Previous live data has been replaced.',
            'success',
        )
    except Exception as exc:
        flash(f'Restore failed: {exc}', 'danger')
    return redirect(url_for('backup.index'))
