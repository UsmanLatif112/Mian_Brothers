"""Shop category / subcategory catalog for inventory purchases."""

from __future__ import annotations

import re

from sqlalchemy.orm import joinedload
from sqlalchemy import or_, text

from app.models import db, ShopCategory, ShopCategoryOption, OtherItem, ItemPurchaseLog
from app.tenancy import apply_agency_filter, stamp_agency


# Only three mains: Fuel tanks, FT Mobile Oil, and Other (qty or liters).
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
        'key': 'ft_mobile',
        'name': 'FT Mobile Oil',
        'unit_mode': 'liters',
        'sort_order': 20,
        'companies': ['Shell', 'Castrol', 'Mobil', 'Total', 'PSO', 'Caltex'],
        'types': [],
    },
    {
        'key': 'other',
        'name': 'Other',
        'unit_mode': 'qty',  # form also offers Liters per purchase
        'sort_order': 30,
        'companies': [],
        'types': [],
    },
]

# Legacy system keys folded into Other
LEGACY_KEYS = ('mobile', 'filter')
MAIN_KEYS = tuple(s['key'] for s in SYSTEM_DEFAULTS)


def slugify_category(name: str) -> str:
    raw = re.sub(r'[^a-z0-9]+', '_', (name or '').lower().strip())
    raw = raw.strip('_')[:40]
    return raw or 'category'


def ensure_shop_categories(agency_id=None):
    """Seed Fuel / FT / Other; fold legacy mobile+filter into Other; deactivate extras."""
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
        if cat.name != spec['name']:
            cat.name = spec['name']
            changed = True
        if cat.unit_mode != spec['unit_mode']:
            cat.unit_mode = spec['unit_mode']
            changed = True
        if cat.sort_order != spec['sort_order']:
            cat.sort_order = spec['sort_order']
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

    # Deactivate legacy mobile / filter and any other non-main categories
    for cat in ShopCategory.query.all():
        if cat.key in MAIN_KEYS:
            continue
        if cat.is_active:
            cat.is_active = False
            changed = True
        if cat.key in LEGACY_KEYS and cat.is_system:
            cat.is_system = False
            changed = True

    if _migrate_legacy_items_to_other():
        changed = True
    if _backfill_stock_units():
        changed = True

    if changed:
        db.session.commit()


def _migrate_legacy_items_to_other():
    """Move mobile/filter stock + purchase logs into category=other."""
    changed = False
    for key in LEGACY_KEYS:
        for item in OtherItem.query.filter_by(category=key).all():
            item.category = 'other'
            if not getattr(item, 'stock_unit', None):
                item.stock_unit = 'qty'
            changed = True
        for log in ItemPurchaseLog.query.filter_by(category=key).all():
            log.category = 'other'
            changed = True
    return changed


def _backfill_stock_units():
    changed = False
    for item in OtherItem.query.all():
        unit = getattr(item, 'stock_unit', None) or 'qty'
        if (item.category or '') == 'ft_mobile':
            if unit != 'liters':
                item.stock_unit = 'liters'
                changed = True
        elif unit not in ('qty', 'liters'):
            item.stock_unit = 'qty'
            changed = True
    return changed


def ensure_other_item_stock_unit_column():
    """Add other_items.stock_unit if missing (SQLite + MySQL)."""
    from sqlalchemy import inspect

    inspector = inspect(db.engine)
    if 'other_items' not in inspector.get_table_names():
        return
    cols = {c['name'] for c in inspector.get_columns('other_items')}
    if 'stock_unit' in cols:
        return
    try:
        with db.engine.begin() as conn:
            conn.execute(text(
                "ALTER TABLE other_items ADD COLUMN stock_unit VARCHAR(10) NOT NULL DEFAULT 'qty'"
            ))
        print('other_items.stock_unit column added.')
    except Exception as e:
        print(f'stock_unit column alter skipped: {e}')


def list_active_categories():
    from app.tenancy import agency_scope

    q = (
        ShopCategory.query
        .options(joinedload(ShopCategory.options))
        .filter_by(is_active=True)
    )
    aid = agency_scope()
    if aid is not None:
        q = q.filter(or_(ShopCategory.agency_id == aid, ShopCategory.agency_id.is_(None)))
    # Prefer main three; hide deactivated legacy even if somehow active
    return [
        c for c in q.order_by(ShopCategory.sort_order.asc(), ShopCategory.name.asc()).all()
        if c.key in MAIN_KEYS
    ]


def category_payload(cat: ShopCategory) -> dict:
    company_opts = [
        {'id': o.id, 'name': o.name}
        for o in sorted(cat.options, key=lambda x: (x.name or '').lower())
        if (o.kind or '') == 'company'
    ]
    type_opts = [
        {'id': o.id, 'name': o.name}
        for o in sorted(cat.options, key=lambda x: (x.name or '').lower())
        if (o.kind or '') == 'type'
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
    cat = opt.category or ShopCategory.query.get(opt.category_id)
    if not cat:
        return False
    if (opt.kind or '') == 'company':
        if apply_agency_filter(
            OtherItem.query.filter_by(category=cat.key, company=opt.name), OtherItem
        ).first():
            return True
        if apply_agency_filter(
            ItemPurchaseLog.query.filter_by(category=cat.key, company=opt.name),
            ItemPurchaseLog,
        ).first():
            return True
    else:
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
