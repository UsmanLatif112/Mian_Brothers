"""Shop category / subcategory catalog for inventory purchases."""

from __future__ import annotations

import re

from sqlalchemy.orm import joinedload

from app.models import db, ShopCategory, ShopCategoryOption, OtherItem, ItemPurchaseLog
from app.tenancy import apply_agency_filter, stamp_agency


SYSTEM_DEFAULTS = [
    {
        'key': 'fuel',
        'name': 'Fuel',
        'unit_mode': 'fuel',
        'sort_order': 10,
        'companies': [],
        'types': [],
    },
    {
        'key': 'mobile',
        'name': 'Mobile (Oil)',
        'unit_mode': 'qty',
        'sort_order': 20,
        'companies': ['Shell', 'Castrol', 'Mobil', 'Total', 'PSO', 'Caltex'],
        'types': ['5W-30', '10W-40', '15W-40', '20W-50', 'ATF', 'Gear Oil', 'Coolant', 'Brake Fluid'],
    },
    {
        'key': 'ft_mobile',
        'name': 'FT Mobile Oil',
        'unit_mode': 'liters',
        'sort_order': 30,
        'companies': ['Shell', 'Castrol', 'Mobil', 'Total', 'PSO', 'Caltex'],
        'types': [],
    },
    {
        'key': 'filter',
        'name': 'Filter',
        'unit_mode': 'qty',
        'sort_order': 40,
        'companies': ['Bosch', 'Mann', 'Fram', 'Toyota', 'Honda'],
        'types': ['Oil Filter', 'Air Filter', 'Fuel Filter', 'Cabin Filter'],
    },
    {
        'key': 'other',
        'name': 'Other Item',
        'unit_mode': 'qty',
        'sort_order': 50,
        'companies': [],
        'types': [],
    },
]


def slugify_category(name: str) -> str:
    raw = re.sub(r'[^a-z0-9]+', '_', (name or '').lower().strip())
    raw = raw.strip('_')[:40]
    return raw or 'category'


def ensure_shop_categories(agency_id=None):
    """Create tables' seed rows (idempotent). Backfill missing system company/type options.

    When agency_id is passed (boot / migrate), seed for that agency.
    Otherwise use the current user's agency scope.
    If a legacy unique on `key` blocks a second agency row, mark the row shared
    (agency_id NULL) so all agencies can use the system catalog.
    """
    from sqlalchemy.exc import IntegrityError

    changed = False
    for spec in SYSTEM_DEFAULTS:
        q = ShopCategory.query.filter_by(key=spec['key'])
        if agency_id is not None:
            q = q.filter_by(agency_id=agency_id)
        else:
            q = apply_agency_filter(q, ShopCategory)
        cat = q.first()
        if not cat:
            # Shared / other-agency row with same key (legacy unique)
            shared = ShopCategory.query.filter_by(key=spec['key']).first()
            if shared is not None:
                if shared.agency_id not in (None, agency_id):
                    shared.agency_id = None
                    changed = True
                cat = shared
            else:
                cat = ShopCategory(
                    key=spec['key'],
                    name=spec['name'],
                    unit_mode=spec['unit_mode'],
                    is_system=True,
                    is_active=True,
                    sort_order=spec['sort_order'],
                    agency_id=agency_id,
                )
                if agency_id is None:
                    stamp_agency(cat)
                db.session.add(cat)
                try:
                    db.session.flush()
                    changed = True
                except IntegrityError:
                    db.session.rollback()
                    cat = ShopCategory.query.filter_by(key=spec['key']).first()
                    if cat and cat.agency_id not in (None, agency_id):
                        cat.agency_id = None
                        changed = True
        if not cat:
            continue
        if not cat.is_system:
            cat.is_system = True
            changed = True
        if not cat.is_active:
            cat.is_active = True
            changed = True
        if not cat.name:
            cat.name = spec['name']
            changed = True
        if not cat.unit_mode:
            cat.unit_mode = spec['unit_mode']
            changed = True

        for kind, names in (('company', spec['companies']), ('type', spec['types'])):
            for name in names:
                exists = ShopCategoryOption.query.filter_by(
                    category_id=cat.id, kind=kind, name=name
                ).first()
                if not exists:
                    db.session.add(ShopCategoryOption(
                        category_id=cat.id, kind=kind, name=name
                    ))
                    changed = True
    if changed:
        db.session.commit()


def list_active_categories():
    from sqlalchemy import or_
    from app.tenancy import agency_scope

    q = (
        ShopCategory.query
        .options(joinedload(ShopCategory.options))
        .filter_by(is_active=True)
    )
    aid = agency_scope()
    if aid is not None:
        # Own agency rows + shared system catalog (agency_id NULL)
        q = q.filter(or_(ShopCategory.agency_id == aid, ShopCategory.agency_id.is_(None)))
    return q.order_by(ShopCategory.sort_order.asc(), ShopCategory.name.asc()).all()


def category_payload(cat: ShopCategory) -> dict:
    company_opts = [
        {'id': o.id, 'name': o.name}
        for o in sorted(cat.options, key=lambda x: (x.name or '').lower())
        if o.kind == 'company'
    ]
    type_opts = [
        {'id': o.id, 'name': o.name}
        for o in sorted(cat.options, key=lambda x: (x.name or '').lower())
        if o.kind == 'type'
    ]
    return {
        'id': cat.id,
        'key': cat.key,
        'name': cat.name,
        'unit_mode': cat.unit_mode,
        'is_system': bool(cat.is_system),
        'companies': [o['name'] for o in company_opts],
        'types': [o['name'] for o in type_opts],
        'company_options': company_opts,
        'type_options': type_opts,
    }


def category_has_linked_activity(cat: ShopCategory) -> bool:
    """True if category key is used by stock or purchase history."""
    if apply_agency_filter(OtherItem.query.filter_by(category=cat.key), OtherItem).first():
        return True
    if apply_agency_filter(
        ItemPurchaseLog.query.filter_by(category=cat.key), ItemPurchaseLog
    ).first():
        return True
    return False


def option_has_linked_activity(opt: ShopCategoryOption) -> bool:
    """True if company/type is used on stock items or purchase logs."""
    cat = opt.category or ShopCategory.query.get(opt.category_id)
    if not cat:
        return False
    if opt.kind == 'company':
        if apply_agency_filter(
            OtherItem.query.filter_by(category=cat.key, company=opt.name), OtherItem
        ).first():
            return True
        if apply_agency_filter(
            ItemPurchaseLog.query.filter_by(category=cat.key, company=opt.name),
            ItemPurchaseLog,
        ).first():
            return True
    elif opt.kind == 'type':
        if apply_agency_filter(
            OtherItem.query.filter_by(category=cat.key, item_type=opt.name), OtherItem
        ).first():
            return True
        if apply_agency_filter(
            ItemPurchaseLog.query.filter_by(category=cat.key, item_type=opt.name),
            ItemPurchaseLog,
        ).first():
            return True
    return False


def unique_category_key(base: str) -> str:
    key = slugify_category(base)
    if not apply_agency_filter(ShopCategory.query.filter_by(key=key), ShopCategory).first():
        return key
    n = 2
    while apply_agency_filter(
        ShopCategory.query.filter_by(key=f'{key}_{n}'), ShopCategory
    ).first():
        n += 1
    return f'{key}_{n}'
