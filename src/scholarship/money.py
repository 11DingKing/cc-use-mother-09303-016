"""金额与汇率的确定性整数运算。

所有金额一律使用最小货币单位（minor units，例如“分”）的整数；
汇率用有理数 num/den 表示，换算结果按汇率口径指定的舍入方式量化到
最小单位。试算、正式授予与审计复算走同一套函数，结果必然一致。
"""
from __future__ import annotations

import re

ROUNDING_MODES = ("DOWN", "UP", "HALF_UP", "HALF_EVEN")

_MINOR_PATTERN = re.compile(r"^(-?\d+)(?:\.(\d{1,2}))?$")


def parse_minor(value: object) -> int:
    """把输入解析为最小单位整数。

    接受整数（已是最小单位）或最多两位小数的十进制字符串；
    拒绝浮点数，避免二进制浮点误差进入账本。
    """
    if isinstance(value, bool):
        raise ValueError("金额必须是整数最小单位或两位小数字符串")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        match = _MINOR_PATTERN.match(value.strip())
        if match:
            whole, frac = match.group(1), match.group(2) or ""
            sign = -1 if whole.startswith("-") else 1
            minor = abs(int(whole)) * 100 + int(frac.ljust(2, "0") or "0")
            return sign * minor
    raise ValueError(f"无法解析的金额：{value!r}")


def convert_minor(amount: int, num: int, den: int, mode: str) -> int:
    """按 num/den 的汇率把最小单位金额换算为目标币种最小单位。"""
    if amount < 0:
        raise ValueError("换算金额不能为负")
    if num <= 0 or den <= 0:
        raise ValueError("汇率必须为正有理数")
    if mode not in ROUNDING_MODES:
        raise ValueError(f"不支持的舍入方式：{mode}")
    quotient, remainder = divmod(amount * num, den)
    if remainder == 0 or mode == "DOWN":
        return quotient
    if mode == "UP":
        return quotient + 1
    twice = remainder * 2
    if mode == "HALF_UP":
        return quotient + (1 if twice >= den else 0)
    # HALF_EVEN：恰好一半时向偶数靠拢
    if twice > den or (twice == den and quotient % 2 == 1):
        return quotient + 1
    return quotient
