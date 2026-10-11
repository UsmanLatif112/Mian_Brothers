"""Agency / multi-tenant scoping helpers.

Roles:
  - super_admin: full app (all agencies)
  - admin: agency owner — all data for their agency; manage team members
  - user: team member — agency data limited to modules allowed by their admin
"""

from flask import abort
from flask_login import current_user

# Operational modules a team member may be granted (admin/super ignore this list).
OPS_MODULES = (
    ('dashboard', 'Dashboard'),
    ('journal', 'Journal'),
    ('sales', 'Sales'),
    ('inventory', 'Inventory'),
    ('purchasing', 'Purchasing'),
    ('expenses', 'Expenses'),
    ('customers', 'Customers'),
    ('vendors', 'Vendors'),
    ('categories', 'Categories'),
    ('account', 'Account'),
    ('pricing', 'Pricing'),
    ('reports', 'Reports'),
)

OPS_MODULE_KEYS = {k for k, _ in OPS_MODULES}

# Blueprint / endpoint prefix → module key
BLUEPRINT_MODULE = {
    'dashboard': 'dashboard',
    'journal': 'journal',
    'sales': 'sales',
    'inventory': 'inventory',
    'purchasing': 'purchasing',
    'expenses': 'expenses',
    'customers': 'customers',
    'vendors': 'vendors',
    'account': 'account',
    'pricing': 'pricing',
}


def is_super_admin():
    """Platform owner — sees all agencies' data."""
    if not current_user.is_authenticated:
        return False
    return (current_user.role or '') == 'super_admin'


def is_agency_admin():
    """Agency admin — all data for their own agency only."""
    if not current_user.is_authenticated:
        return False
    return (current_user.role or '') == 'admin'


def is_agency_manager():
    """Super admin or agency admin (can manage team / sync tools in scope)."""
    return is_super_admin() or is_agency_admin()


def agency_scope():
    """None = no filter (super_admin sees all). Else agency_id for admin/user."""
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


def parse_allowed_modules(raw):
    """Normalize allowed_modules string → set of keys, or None for 'all'."""
    if raw is None or str(raw).strip() in ('', '*'):
        return None
    keys = {p.strip() for p in str(raw).split(',') if p.strip()}
    keys &= OPS_MODULE_KEYS
    return keys or None


def serialize_allowed_modules(keys):
    if keys is None:
        return '*'
    cleaned = sorted(k for k in keys if k in OPS_MODULE_KEYS)
    return ','.join(cleaned) if cleaned else '*'


def user_allowed_modules(user=None):
    """Modules the user may open. None = all operational modules."""
    u = user if user is not None else current_user
    if not getattr(u, 'is_authenticated', False) and user is None:
        return set()
    role = getattr(u, 'role', '') or ''
    if role in ('super_admin', 'admin'):
        return None  # unrestricted within their role scope
    return parse_allowed_modules(getattr(u, 'allowed_modules', None))


def can_access_module(module_key, user=None):
    """Whether the user may use an operational module."""
    if not module_key:
        return True
    allowed = user_allowed_modules(user)
    if allowed is None:
        return True
    return module_key in allowed


def module_for_endpoint(endpoint):
    """Map Flask endpoint to an OPS module key, or None if not module-gated."""
    if not endpoint:
        return None
    if endpoint.startswith('dashboard.reports') or endpoint == 'dashboard.reports':
        return 'reports'
    if endpoint.startswith('inventory.categories'):
        return 'categories'
    bp = endpoint.split('.', 1)[0]
    return BLUEPRINT_MODULE.get(bp)
