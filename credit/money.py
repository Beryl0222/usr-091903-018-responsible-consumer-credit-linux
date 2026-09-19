"""金额工具：对内一律使用整数分（cents），对外使用两位小数字符串。

避免二进制浮点误差。利息与摊还计算使用 Decimal。
"""

from decimal import ROUND_HALF_UP, Decimal, getcontext

getcontext().prec = 28

from credit.dates import add_months

CENT = Decimal("0.01")


def yuan_to_cents(value) -> int:
    """元（float/str/Decimal）→ 分（int），四舍五入到分。"""
    if value is None:
        raise ValueError("金额不能为空")
    if isinstance(value, bool):
        raise ValueError("金额不能是布尔值")
    dec = Decimal(str(value))
    if dec < 0:
        raise ValueError("金额不能为负")
    return int((dec * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def cents_to_yuan(cents: int) -> str:
    return str((Decimal(cents) / 100).quantize(CENT, rounding=ROUND_HALF_UP))


def cents_to_decimal(cents: int) -> Decimal:
    return Decimal(cents) / 100


def round_cents(dec: Decimal) -> int:
    return int((dec * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def annuity_payment_cents(principal_cents: int, monthly_rate: Decimal, months: int) -> int:
    """等额本息月供（分）。月利率为 0 时退化为等额本金。"""
    principal = cents_to_decimal(principal_cents)
    if monthly_rate == 0:
        return round_cents(principal / months)
    factor = (1 + monthly_rate) ** months
    payment = principal * monthly_rate * factor / (factor - 1)
    return round_cents(payment)


def build_schedule(principal_cents: int, annual_rate: Decimal, months: int,
                   start_date) -> list[dict]:
    """生成等额本息还款计划，末期吸收尾差。

    年利率为单利年化（APR），月利率 = 年利率 / 12。
    """
    monthly_rate = annual_rate / 12
    payment = annuity_payment_cents(principal_cents, monthly_rate, months)
    remaining = Decimal(principal_cents) / 100
    rows = []
    for seq in range(1, months + 1):
        interest = round_cents(remaining * monthly_rate)
        if seq == months:
            principal_part = round_cents(remaining)
            pay = principal_part + interest
        else:
            principal_part = min(payment - interest, round_cents(remaining))
            pay = payment
        remaining -= Decimal(principal_part) / 100
        due_date = add_months(start_date, seq) if start_date else None
        rows.append({
            "seq": seq,
            "due_date": due_date.isoformat() if due_date else None,
            "payment_cents": pay,
            "principal_cents": principal_part,
            "interest_cents": interest,
            "status": "scheduled",
        })
    return rows
