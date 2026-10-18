"""Dip stick calculator — admin / super_admin only."""
import re

from flask import render_template, request, jsonify, flash, redirect, url_for
from flask_login import login_required

from app.dip import dip_bp
from app.decorators import role_required
from app.models import db, DipChart
from app.tenancy import apply_agency_filter, stamp_agency, require_agency_access, get_write_agency_id


def _normalize_kind(raw):
    kind = (raw or '').strip().lower()
    if kind in ('petrol', 'diesel'):
        return kind
    return None


def _charts_for(kind):
    return (
        apply_agency_filter(
            DipChart.query.filter_by(fuel_kind=kind),
            DipChart,
            include_shared=True,
        )
        .order_by(DipChart.dip.asc(), DipChart.id.asc())
        .all()
    )


def closest_dip_row(kind, dip_value):
    """Pick chart row with dip nearest to dip_value (tie → lower dip)."""
    rows = _charts_for(kind)
    if not rows:
        return None
    target = float(dip_value)
    best = None
    best_dist = None
    for row in rows:
        d = float(row.dip or 0)
        dist = abs(d - target)
        if best is None or dist < best_dist - 1e-12 or (
            abs(dist - best_dist) <= 1e-12 and d < float(best.dip or 0)
        ):
            best = row
            best_dist = dist
    return best


@dip_bp.route('/')
@login_required
@role_required('super_admin', 'admin')
def index():
    petrol = _charts_for('petrol')
    diesel = _charts_for('diesel')
    return render_template(
        'dip/index.html',
        petrol_rows=petrol,
        diesel_rows=diesel,
    )


@dip_bp.route('/api/calculate', methods=['POST'])
@login_required
@role_required('super_admin', 'admin')
def api_calculate():
    data = request.get_json(silent=True) or {}
    kind = _normalize_kind(data.get('fuel_kind') or data.get('kind'))
    if not kind:
        return jsonify({'ok': False, 'error': 'Select Petrol or Diesel.'}), 400
    try:
        dip_in = float(data.get('dip'))
        if dip_in <= 0:
            raise ValueError('Dip must be greater than zero.')
    except (TypeError, ValueError) as e:
        return jsonify({'ok': False, 'error': f'Invalid dip: {e}'}), 400

    row = closest_dip_row(kind, dip_in)
    if not row:
        return jsonify({'ok': False, 'error': f'No {kind} dip chart entries yet.'}), 400

    chart_dip = float(row.dip or 0)
    chart_liters = float(row.liters or 0)
    if chart_dip <= 0:
        return jsonify({'ok': False, 'error': 'Chart dip cannot be zero.'}), 400

    # liters_per_dip_unit * measured_dip
    estimated = (chart_liters / chart_dip) * dip_in
    return jsonify({
        'ok': True,
        'fuel_kind': kind,
        'input_dip': round(dip_in, 3),
        'closest_dip': round(chart_dip, 3),
        'closest_liters': round(chart_liters, 3),
        'estimated_liters': round(estimated, 3),
        'formula': f'({chart_liters:g} ÷ {chart_dip:g}) × {dip_in:g}',
    })


@dip_bp.route('/api/rows', methods=['GET'])
@login_required
@role_required('super_admin', 'admin')
def api_rows():
    kind = _normalize_kind(request.args.get('fuel_kind') or request.args.get('kind'))
    if not kind:
        return jsonify({'ok': False, 'error': 'fuel_kind required'}), 400
    rows = [
        {
            'id': r.id,
            'dip': float(r.dip or 0),
            'liters': float(r.liters or 0),
        }
        for r in _charts_for(kind)
    ]
    return jsonify({'ok': True, 'fuel_kind': kind, 'rows': rows})


@dip_bp.route('/api/rows', methods=['POST'])
@login_required
@role_required('super_admin', 'admin')
def api_add_row():
    data = request.get_json(silent=True) or {}
    kind = _normalize_kind(data.get('fuel_kind') or data.get('kind'))
    if not kind:
        return jsonify({'ok': False, 'error': 'Select Petrol or Diesel.'}), 400
    try:
        dip = float(data.get('dip'))
        liters = float(data.get('liters'))
        if dip <= 0 or liters < 0:
            raise ValueError('Dip must be > 0 and liters ≥ 0.')
    except (TypeError, ValueError) as e:
        return jsonify({'ok': False, 'error': f'Invalid values: {e}'}), 400

    aid = get_write_agency_id()
    existing = (
        apply_agency_filter(
            DipChart.query.filter_by(fuel_kind=kind, dip=dip),
            DipChart,
        ).first()
    )
    if existing:
        existing.liters = liters
        db.session.commit()
        return jsonify({
            'ok': True,
            'updated': True,
            'row': {'id': existing.id, 'dip': float(existing.dip), 'liters': float(existing.liters)},
        })

    row = stamp_agency(DipChart(fuel_kind=kind, dip=dip, liters=liters, agency_id=aid))
    db.session.add(row)
    try:
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        return jsonify({'ok': False, 'error': f'Could not save: {e}'}), 400
    return jsonify({
        'ok': True,
        'updated': False,
        'row': {'id': row.id, 'dip': float(row.dip), 'liters': float(row.liters)},
    })


@dip_bp.route('/api/rows/<int:row_id>', methods=['DELETE', 'POST'])
@login_required
@role_required('super_admin', 'admin')
def api_delete_row(row_id):
    # Allow POST with _method=delete for simple forms
    if request.method == 'POST' and (request.form.get('_method') or '').lower() != 'delete':
        data = request.get_json(silent=True) or {}
        if (data.get('_method') or '').lower() != 'delete' and not data.get('delete'):
            return jsonify({'ok': False, 'error': 'Use DELETE.'}), 405

    row = DipChart.query.get_or_404(row_id)
    require_agency_access(row)
    db.session.delete(row)
    db.session.commit()
    return jsonify({'ok': True})


def _parse_bulk_pairs(raw_text):
    """Parse lines like '220-818', '220 818', or '220,818' into (dip, liters)."""
    pairs = []
    errors = []
    for i, line in enumerate((raw_text or '').splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        parts = re.split(r'[-–,;\s]+', line)
        parts = [p for p in parts if p]
        if len(parts) < 2:
            errors.append(f'Line {i}: need dip and liters (e.g. 220-818)')
            continue
        try:
            dip = float(parts[0])
            liters = float(parts[1])
            if dip <= 0 or liters < 0:
                raise ValueError('out of range')
            pairs.append((dip, liters))
        except (TypeError, ValueError):
            errors.append(f'Line {i}: invalid numbers "{line}"')
    return pairs, errors


def _upsert_pairs(kind, pairs):
    """Insert/update chart rows. Returns (added, updated)."""
    aid = get_write_agency_id()
    added = updated = 0
    for dip, liters in pairs:
        existing = apply_agency_filter(
            DipChart.query.filter_by(fuel_kind=kind, dip=dip), DipChart
        ).first()
        if existing:
            existing.liters = liters
            updated += 1
        else:
            db.session.add(stamp_agency(DipChart(
                fuel_kind=kind, dip=dip, liters=liters, agency_id=aid
            )))
            added += 1
    if added or updated:
        db.session.commit()
    return added, updated


@dip_bp.route('/rows/add', methods=['POST'])
@login_required
@role_required('super_admin', 'admin')
def add_row_form():
    """Add one or many chart rows (row inputs, bulk text, or single pair)."""
    kind = _normalize_kind(request.form.get('fuel_kind'))
    if not kind:
        flash('Invalid fuel type.', 'danger')
        return redirect(url_for('dip.index'))

    pairs = []
    errors = []

    dips = request.form.getlist('dip')
    liters_list = request.form.getlist('liters')
    if dips or liters_list:
        for i, raw_dip in enumerate(dips):
            raw_dip = (raw_dip or '').strip()
            raw_liters = (liters_list[i] if i < len(liters_list) else '').strip()
            if not raw_dip and not raw_liters:
                continue
            try:
                dip = float(raw_dip)
                liters = float(raw_liters)
                if dip <= 0 or liters < 0:
                    raise ValueError('out of range')
                pairs.append((dip, liters))
            except (TypeError, ValueError):
                errors.append(f'Row {i + 1}: invalid dip/liters')

    if not pairs:
        bulk = (request.form.get('bulk_rows') or '').strip()
        if bulk:
            pairs, errors = _parse_bulk_pairs(bulk)

    if not pairs:
        flash(errors[0] if errors else 'Enter at least one dip and liters.', 'danger')
        return redirect(url_for('dip.index', tab=kind))

    added, updated = _upsert_pairs(kind, pairs)
    msg = f'Added {added}, updated {updated} {kind} entr{"y" if (added + updated) == 1 else "ies"}.'
    if errors:
        msg += f' Skipped {len(errors)} invalid row(s).'
    flash(msg, 'success' if (added or updated) else 'warning')
    return redirect(url_for('dip.index', tab=kind))


@dip_bp.route('/rows/<int:row_id>/edit', methods=['POST'])
@login_required
@role_required('super_admin', 'admin')
def edit_row_form(row_id):
    row = DipChart.query.get_or_404(row_id)
    require_agency_access(row)
    kind = row.fuel_kind
    try:
        dip = float(request.form.get('dip'))
        liters = float(request.form.get('liters'))
        if dip <= 0 or liters < 0:
            raise ValueError('invalid')
    except (TypeError, ValueError):
        flash('Invalid dip entry.', 'danger')
        return redirect(url_for('dip.index', tab=kind))

    clash = apply_agency_filter(
        DipChart.query.filter_by(fuel_kind=kind, dip=dip), DipChart
    ).filter(DipChart.id != row.id).first()
    if clash:
        flash(f'A {kind} entry for dip {dip:g} already exists.', 'danger')
        return redirect(url_for('dip.index', tab=kind))

    row.dip = dip
    row.liters = liters
    db.session.commit()
    flash(f'Updated {kind} dip {dip:g} → {liters:g}.', 'success')
    return redirect(url_for('dip.index', tab=kind))


@dip_bp.route('/rows/<int:row_id>/delete', methods=['POST'])
@login_required
@role_required('super_admin', 'admin')
def delete_row_form(row_id):
    row = DipChart.query.get_or_404(row_id)
    require_agency_access(row)
    kind = row.fuel_kind
    db.session.delete(row)
    db.session.commit()
    flash('Dip entry removed.', 'success')
    return redirect(url_for('dip.index', tab=kind))
