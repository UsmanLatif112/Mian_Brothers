"""
Refresh customer/vendor settle labels to include expense name + description.

Usage:
  PYTHONPATH=. python scripts/repair_expense_settle_labels.py
"""
from __future__ import annotations

from app import create_app
from app.models import db, Expense, CreditSale, ItemPurchaseLog
from app.utils import expense_settle_remark


def repair():
    updated_cs = 0
    updated_logs = 0
    expenses = Expense.query.filter(
        db.or_(
            Expense.settle_credit_sale_id.isnot(None),
            Expense.settle_purchase_log_id.isnot(None),
        )
    ).all()

    for expense in expenses:
        label = expense_settle_remark(
            expense.name,
            getattr(expense, 'settle_note', None),
            getattr(expense, 'description', None),
        )
        cs_id = getattr(expense, 'settle_credit_sale_id', None)
        if cs_id:
            cs = CreditSale.query.get(cs_id)
            if cs is not None:
                new_remark = (label or '')[:255]
                if (cs.remarks or '') != new_remark:
                    cs.remarks = new_remark
                    updated_cs += 1

        log_id = getattr(expense, 'settle_purchase_log_id', None)
        if log_id:
            log = ItemPurchaseLog.query.get(log_id)
            if log is not None:
                new_name = (label or 'Settle')[:100]
                if (log.item_name or '') != new_name:
                    log.item_name = new_name
                    updated_logs += 1

    db.session.commit()
    return updated_cs, updated_logs, len(expenses)


def main():
    app = create_app()
    with app.app_context():
        cs_n, log_n, total = repair()
        print(
            f'Updated {cs_n} customer settle remark(s) and {log_n} vendor settle '
            f'label(s) across {total} settled expense(s).'
        )


if __name__ == '__main__':
    main()
