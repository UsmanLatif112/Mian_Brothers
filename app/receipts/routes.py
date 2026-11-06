"""Boltwood-style receipt view pages (Back + Print) with agency branding."""

from flask import render_template, abort
from flask_login import login_required, current_user
from app.receipts import receipts_bp
from app.models import (
    Agency, ItemPurchaseLog, CreditSale, Payment, VendorPayment, Sale, Customer, Vendor,
)
from app.tenancy import require_agency_access


def _agency_for(obj):
    aid = getattr(obj, 'agency_id', None) or (
        current_user.agency_id if current_user.is_authenticated else None
    )
    if not aid:
        return None
    return Agency.query.get(aid)


def _money(value):
    from app.formatters import format_number
    return format_number(value, max_decimals=2, default='0')


@receipts_bp.route('/purchase/<int:log_id>')
@login_required
def purchase(log_id):
    log = ItemPurchaseLog.query.get_or_404(log_id)
    require_agency_access(log)
    agency = _agency_for(log)
    qty = log.quantity if log.quantity is not None else log.liters
    batch = float(log.cost_price or 0) * float(qty or 0)
    pay_status = (log.payment_status or 'unpaid').lower()
    unit = 'L' if log.liters is not None and (log.quantity is None or float(log.liters or 0) > 0 and not log.quantity) else 'pcs'
    if log.category == 'fuel' or (log.liters is not None and log.quantity is None):
        unit = 'L'
    parts = [log.item_name or 'Item']
    if log.company:
        parts.append(f"({log.company})")
    if qty is not None:
        parts.append(f"· {float(qty):,.2f} {unit}")
    if log.category:
        parts.append(f"· {log.category}")
    vendor = None
    if log.vendor_id:
        vendor = Vendor.query.get(log.vendor_id)
    pending = float(vendor.current_balance_payable or 0) if vendor else (
        0.0 if pay_status == 'paid' else batch
    )
    return render_template(
        'print/receipt.html',
        agency=agency,
        subtitle='Purchase Receipt',
        title='Purchase',
        ref=f'Purchase #{log.id}',
        date=log.entry_date.strftime('%Y-%m-%d %H:%M') if log.entry_date else '—',
        party_label='Vendor',
        party=(vendor.name if vendor else None) or log.vendor or '—',
        desc=' '.join(parts),
        status=pay_status.upper(),
        amount=_money(batch),
        total=_money(batch),
        paid=_money(batch if pay_status == 'paid' else 0),
        due=_money(pending),
        balance_label='Pending payable',
        back_url=None,
    )


def _sale_particulars(cs):
    """Build Particulars text for a CreditSale invoice (same style as customer ledger)."""
    item = (cs.item_name or 'Item').strip()
    et = (cs.entry_type or 'sale').lower()
    if et != 'sale':
        bits = [item]
        if cs.remarks and (cs.remarks or '').strip() != item:
            bits.append((cs.remarks or '').strip())
        return ' · '.join(bits) if bits else 'Sale'

    qty = float(cs.liters or 0)
    disc = float(cs.discount or 0)
    if cs.is_fuel or (cs.other_item and cs.other_item.tracks_liters()):
        unit = 'L'
    else:
        unit = 'pcs'

    if qty:
        desc = f"{qty:,.2f} {unit} of {item}"
    else:
        desc = item
    if disc > 0:
        desc += f" (−{_money(disc)} discount)"
    if cs.vehicle_number:
        desc += f" · Veh {cs.vehicle_number}"
    if cs.remarks:
        desc += f" · {cs.remarks}"
    return desc


@receipts_bp.route('/sale/<int:sale_id>')
@login_required
def sale(sale_id):
    cs = CreditSale.query.get(sale_id)
    if cs:
        require_agency_access(cs)
        agency = _agency_for(cs)
        party = cs.customer.name if cs.customer else 'Walk-in'
        status = (cs.payment_status or 'unpaid').upper()
        total = float(cs.amount or 0)
        paid = float(cs.amount_paid or 0) + float(cs.overpayment or 0)
        pending = (
            float(cs.customer.current_balance_due or 0)
            if cs.customer
            else max(total - float(cs.amount_paid or 0), 0)
        )
        return render_template(
            'print/receipt.html',
            agency=agency,
            subtitle='Sale Invoice',
            title='Sale',
            ref=f'Sale #{cs.id}',
            date=cs.sale_date.strftime('%Y-%m-%d') if cs.sale_date else '—',
            party_label='Customer',
            party=party,
            desc=_sale_particulars(cs),
            status=status,
            amount=_money(total),
            total=_money(total),
            paid=_money(paid),
            due=_money(pending),
            balance_label='Pending receivable',
            back_url=None,
        )

    legacy = Sale.query.get_or_404(sale_id)
    require_agency_access(legacy)
    agency = _agency_for(legacy)
    party = legacy.customer.name if legacy.customer else 'Walk-in'
    total = float(legacy.total_amount or 0)
    fuel_name = legacy.fuel_type.name if legacy.fuel_type else 'Fuel'
    pending = (
        float(legacy.customer.current_balance_due or 0)
        if legacy.customer
        else (0.0 if (legacy.payment_type or '').lower() == 'cash' else total)
    )
    return render_template(
        'print/receipt.html',
        agency=agency,
        subtitle='Sale Invoice',
        title='Sale',
        ref=f'Sale #{legacy.id}',
        date=legacy.sale_date.strftime('%Y-%m-%d %H:%M') if legacy.sale_date else '—',
        party_label='Customer',
        party=party,
        desc=f"{float(legacy.liters or 0):,.2f} L of {fuel_name}",
        status=(legacy.payment_type or 'cash').upper(),
        amount=_money(total),
        total=_money(total),
        paid=_money(total if (legacy.payment_type or '').lower() == 'cash' else 0),
        due=_money(pending),
        balance_label='Pending receivable',
        back_url=None,
    )


@receipts_bp.route('/customer-payment/<int:payment_id>')
@login_required
def customer_payment(payment_id):
    payment = Payment.query.get_or_404(payment_id)
    require_agency_access(payment)
    customer = Customer.query.get(payment.customer_id)
    if customer:
        require_agency_access(customer)
    agency = _agency_for(payment) or _agency_for(customer)
    amt = float(payment.amount_paid or 0)
    pending = float(customer.current_balance_due or 0) if customer else 0.0
    return render_template(
        'print/receipt.html',
        agency=agency,
        subtitle='Customer Receipt',
        title='Payment',
        ref=f'Payment #{payment.id}',
        date=payment.payment_date.strftime('%Y-%m-%d %H:%M') if payment.payment_date else '—',
        party_label='Customer',
        party=customer.name if customer else '—',
        desc=payment.note or payment.method or 'Payment',
        status='PAID',
        amount=_money(amt),
        total=_money(amt),
        paid=_money(amt),
        due=_money(pending),
        balance_label='Pending receivable',
        back_url=None,
    )


@receipts_bp.route('/vendor-payment/<int:payment_id>')
@login_required
def vendor_payment(payment_id):
    payment = VendorPayment.query.get_or_404(payment_id)
    require_agency_access(payment)
    vendor = Vendor.query.get(payment.vendor_id)
    if vendor:
        require_agency_access(vendor)
    agency = _agency_for(payment) or _agency_for(vendor)
    amt = float(payment.amount_paid or 0)
    pending = float(vendor.current_balance_payable or 0) if vendor else 0.0
    return render_template(
        'print/receipt.html',
        agency=agency,
        subtitle='Vendor Receipt',
        title='Payment',
        ref=f'Payment #{payment.id}',
        date=payment.payment_date.strftime('%Y-%m-%d %H:%M') if payment.payment_date else '—',
        party_label='Vendor',
        party=vendor.name if vendor else '—',
        desc=payment.note or payment.method or 'Payment',
        status='PAID',
        amount=_money(amt),
        total=_money(amt),
        paid=_money(amt),
        due=_money(pending),
        balance_label='Pending payable',
        back_url=None,
    )


@receipts_bp.route('/credit-sale/<int:sale_id>')
@login_required
def credit_sale(sale_id):
    """Alias used from customer ledger sale rows."""
    return sale(sale_id)
