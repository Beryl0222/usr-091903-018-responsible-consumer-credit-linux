"""金额、日期与摊还计算工具。金额一律以“分”整数存储。"""

import calendar
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

CENT = Decimal("0.01")


def yuan_to_cents(value):
    if value is None:
        raise ValueError("amount_required")
    return int(Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP) * 100)


def cents_to_yuan(cents):
    return round(cents / 100, 2)


def round_cents(value):
    return int(Decimal(str(value)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def add_months(day, months):
    month_index = day.year * 12 + (day.month - 1) + months
    year, month = divmod(month_index, 12)
    month += 1
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, min(day.day, last_day))


def monthly_rate(annual_rate):
    return Decimal(str(annual_rate)) / Decimal(12)


def monthly_payment_cents(principal_cents, annual_rate, months):
    """等额本息月供（四舍五入到分）。"""
    if months <= 0:
        raise ValueError("term_required")
    if annual_rate == 0:
        return -(-principal_cents // months)  # 向上取整，保证总额覆盖本金
    r = monthly_rate(annual_rate)
    factor = (Decimal(1) + r) ** months
    payment = Decimal(principal_cents) * r * factor / (factor - 1)
    return round_cents(payment)


def max_principal_for_payment(payment_cents, annual_rate, months):
    """由可承受月供反推最大本金。"""
    if payment_cents <= 0:
        return 0
    if annual_rate == 0:
        return payment_cents * months
    r = monthly_rate(annual_rate)
    factor = (Decimal(1) + r) ** months
    principal = Decimal(payment_cents) * (factor - 1) / (r * factor)
    return int(Decimal(principal).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def build_schedule(principal_cents, annual_rate, months, first_due):
    """生成逐期计划：每期 (period_no, due_date, principal_cents, interest_cents)。

    末期吸收四舍五入残差，保证本金合计精确等于 principal。
    """
    payment = monthly_payment_cents(principal_cents, annual_rate, months)
    r = monthly_rate(annual_rate)
    remaining = principal_cents
    rows = []
    due = first_due
    for period in range(1, months + 1):
        interest = round_cents(Decimal(remaining) * r)
        if period == months:
            principal_part = remaining
        else:
            principal_part = payment - interest
            if principal_part > remaining:
                principal_part = remaining
        rows.append((period, due, principal_part, interest))
        remaining -= principal_part
        due = add_months(due, 1)
    assert remaining == 0
    return rows
