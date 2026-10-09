"""Agency / multi-tenant scoping helpers (Boltwood-style)."""

from flask import abort
from flask_login import current_user


def is_super_admin():
    """Platform owner — sees all agencies' data."""
    if not current_user.is_authenticated:
        return False
    return current_user.role in ('super_admin', 'admin')


def agency_scope():
    """None = no filter (super_admin sees all). Else agency_id for the current user."""
    if not current_user.is_authenticated:
        return None
    if is_super_admin():
        return None
    return current_user.agency_id


def apply_agency_filter(query, model, include_shared=False):
    """Restrict a SQLAlchemy query to the current user's agency (unless super_admin).

    When include_shared=True, also include rows with agency_id IS NULL (shared catalog).
    """
    from sqlalchemy import or_

    aid = agency_scope()
    if aid is None:
        return query
    col = getattr(model, 'agency_id', None)
    if col is None:
        return query
    if include_shared:
        return query.filter(or_(col == aid, col.is_(None)))
    return query.filter(col == aid)


def stamp_agency(obj):
    """Set agency_id on new records from the current user when missing."""
    if getattr(obj, 'agency_id', None) is not None:
        return obj
    if current_user.is_authenticated and current_user.agency_id:
        obj.agency_id = current_user.agency_id
    return obj


def require_agency_access(obj):
    """404 if the object belongs to another agency (super_admin always OK)."""
    if obj is None:
        abort(404)
    if is_super_admin():
        return obj
    if not current_user.is_authenticated:
        abort(404)
    obj_aid = getattr(obj, 'agency_id', None)
    if obj_aid is not None and obj_aid != current_user.agency_id:
        abort(404)
    return obj


def get_write_agency_id():
    """Agency id to stamp on creates."""
    if current_user.is_authenticated and current_user.agency_id:
        return current_user.agency_id
    return None
