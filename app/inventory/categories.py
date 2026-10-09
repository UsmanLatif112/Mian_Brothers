"""Shop category / subcategory catalog for inventory purchases."""

from __future__ import annotations

import re

from sqlalchemy.orm import joinedload

from app.models import db, ShopCategory, ShopCategoryOption, OtherItem, ItemPurchaseLog


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


def ensure_shop_categories():
    """Create tables' seed rows (idempotent). Backfill missing system company/type options."""
    changed = False
    for spec in SYSTEM_DEFAULTS:
        cat = ShopCategory.query.filter_by(key=spec['key']).first()
        if not cat:
            cat = ShopCategory(
                key=spec['key'],
                name=spec['name'],
                unit_mode=spec['unit_mode'],
                is_system=True,
                is_active=True,
                sort_order=spec['sort_order'],
            )
            db.session.add(cat)
            db.session.flush()
            changed = True
        else:
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
    return (
        ShopCategory.query
        .options(joinedload(ShopCategory.options))
        .filter_by(is_active=True)
        .order_by(ShopCategory.sort_order.asc(), ShopCategory.name.asc())
        .all()
    )


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
    if OtherItem.query.filter_by(category=cat.key).first():
        return True
    if ItemPurchaseLog.query.filter_by(category=cat.key).first():
        return True
    return False


def option_has_linked_activity(opt: ShopCategoryOption) -> bool:
    """True if company/type is used on stock items or purchase logs."""
    cat = opt.category or ShopCategory.query.get(opt.category_id)
    if not cat:
        return False
    if opt.kind == 'company':
        if OtherItem.query.filter_by(category=cat.key, company=opt.name).first():
            return True
        if ItemPurchaseLog.query.filter_by(category=cat.key, company=opt.name).first():
            return True
    elif opt.kind == 'type':
        if OtherItem.query.filter_by(category=cat.key, item_type=opt.name).first():
            return True
        if ItemPurchaseLog.query.filter_by(category=cat.key, item_type=opt.name).first():
            return True
    return False


def unique_category_key(base: str) -> str:
    key = slugify_category(base)
    if not ShopCategory.query.filter_by(key=key).first():
        return key
    n = 2
    while ShopCategory.query.filter_by(key=f'{key}_{n}').first():
        n += 1
    return f'{key}_{n}'
