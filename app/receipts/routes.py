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
    try:
        return f"{float(value or 0):,.2f}"
    except (TypeError, ValueError):
        return "0.00"


@receipts_bp.route('/purchase/<int:log_id>')
@login_required
def purchase(log_id):
    log = ItemPurchaseLog.query.get_or_404(log_id)
    require_agency_access(log)
    agency = _agency_for(log)
    qty = log.quantity if log.quantity is not None else log.liters
    batch = float(log.cost_price or 0) * float(qty or 0)
    pay_status = (log.payment_status or 'unpaid').lower()
    return render_template(
        'print/receipt.html',
        agency=agency,
        subtitle='Purchase Receipt',
        title='Purchase',
        ref=f'Purchase #{log.id}',
        date=log.entry_date.strftime('%Y-%m-%d %H:%M') if log.entry_date else '—',
        party_label='Vendor',
        party=log.vendor or '—',
        desc=f"{log.item_name}{' (' + log.company + ')' if log.company else ''} · {log.category}",
        status=pay_status.upper(),
        amount=_money(batch),
        total=_money(batch),
        paid=_money(batch if pay_status == 'paid' else 0),
        due=_money(0 if pay_status == 'paid' else batch),
        back_url=None,
    )


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
        return render_template(
            'print/receipt.html',
            agency=agency,
            subtitle='Sale Receipt',
            title=cs.item_name,
            ref=f'Sale #{cs.id}',
            date=cs.sale_date.strftime('%Y-%m-%d') if cs.sale_date else '—',
            party_label='Customer',
            party=party,
            desc=cs.remarks or cs.entry_type or 'Sale',
            status=status,
            amount=_money(total),
            total=_money(total),
            paid=_money(paid),
            due=_money(max(total - float(cs.amount_paid or 0), 0)),
            back_url=None,
        )

    legacy = Sale.query.get_or_404(sale_id)
    require_agency_access(legacy)
    agency = _agency_for(legacy)
    party = legacy.customer.name if legacy.customer else 'Walk-in'
    total = float(legacy.total_amount or 0)
    return render_template(
        'print/receipt.html',
        agency=agency,
        subtitle='Sale Receipt',
        title='Fuel Sale',
        ref=f'Sale #{legacy.id}',
        date=legacy.sale_date.strftime('%Y-%m-%d %H:%M') if legacy.sale_date else '—',
        party_label='Customer',
        party=party,
        desc=f"{legacy.liters} L @ {legacy.price_per_liter}",
        status=(legacy.payment_type or 'cash').upper(),
        amount=_money(total),
        total=_money(total),
        paid=_money(total if (legacy.payment_type or '').lower() == 'cash' else 0),
        due=_money(0 if (legacy.payment_type or '').lower() == 'cash' else total),
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
        due=_money(0),
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
        due=_money(0),
        back_url=None,
    )


@receipts_bp.route('/credit-sale/<int:sale_id>')
@login_required
def credit_sale(sale_id):
    """Alias used from customer ledger sale rows."""
    return sale(sale_id)
