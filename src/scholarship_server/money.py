"""金额与汇率：整数最小货币单位 + 有理数尾差。

所有资金以整数最小单位（如分）入账；汇率换算产生的尾差以有理数
精确记录并归集到固定批次，保证任一轮次都可以精确对账。
"""
from __future__ import annotations

from fractions import Fraction


def round_half_up(value: Fraction) -> int:
    """四舍五入到最近的整数最小单位（半数向上）。"""
    return (2 * value.numerator + value.denominator) // (2 * value.denominator)


def fx_convert(amount_minor: int, num: int, den: int) -> tuple[int, Fraction]:
    """按 num/den 汇率换算，返回 (入账整数, 尾差)。

    尾差 = 精确值 - 入账整数，以目标币种最小单位计，可正可负；
    入账整数与尾差之和恒等于精确值，账务因此始终可复算。
    """
    exact = Fraction(amount_minor * num, den)
    converted = round_half_up(exact)
    return converted, exact - converted


def to_json_amount(value: Fraction) -> int | str:
    """序列化金额：整数直接输出，分数以 "分子/分母" 精确输出。"""
    if value.denominator == 1:
        return value.numerator
    return f"{value.numerator}/{value.denominator}"
