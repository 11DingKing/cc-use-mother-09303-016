"""金额与汇率换算的确定性测试。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from scholarship.money import convert_minor, parse_minor


class ParseMinorTest(unittest.TestCase):
    def test_integer_is_minor_units(self) -> None:
        self.assertEqual(parse_minor(10050), 10050)

    def test_decimal_string(self) -> None:
        self.assertEqual(parse_minor("100.50"), 10050)
        self.assertEqual(parse_minor("0.01"), 1)
        self.assertEqual(parse_minor("7"), 700)
        self.assertEqual(parse_minor("-3.25"), -325)

    def test_rejects_float_and_garbage(self) -> None:
        for value in (100.5, True, "abc", "1.005", None, "1."):
            with self.assertRaises(ValueError, msg=repr(value)):
                parse_minor(value)


class ConvertMinorTest(unittest.TestCase):
    def test_exact_conversion(self) -> None:
        # 1000.00 EUR × 1.08 = 1080.00 USD
        self.assertEqual(convert_minor(100_000, 108, 100, "HALF_EVEN"), 108_000)

    def test_rounding_modes(self) -> None:
        # 105 × 1/2 = 52.5
        self.assertEqual(convert_minor(105, 1, 2, "DOWN"), 52)
        self.assertEqual(convert_minor(105, 1, 2, "UP"), 53)
        self.assertEqual(convert_minor(105, 1, 2, "HALF_UP"), 53)
        self.assertEqual(convert_minor(105, 1, 2, "HALF_EVEN"), 52)  # 52 是偶数
        # 115 × 1/2 = 57.5
        self.assertEqual(convert_minor(115, 1, 2, "HALF_EVEN"), 58)
        self.assertEqual(convert_minor(115, 1, 2, "HALF_UP"), 58)

    def test_fractional_rounding_is_deterministic(self) -> None:
        # 33333 × 108/100 = 35999.64 → 36000
        self.assertEqual(convert_minor(33_333, 108, 100, "HALF_EVEN"), 36_000)
        self.assertEqual(convert_minor(33_333, 108, 100, "DOWN"), 35_999)

    def test_rejects_invalid_input(self) -> None:
        with self.assertRaises(ValueError):
            convert_minor(-1, 1, 1, "DOWN")
        with self.assertRaises(ValueError):
            convert_minor(1, 0, 1, "DOWN")
        with self.assertRaises(ValueError):
            convert_minor(1, 1, 0, "DOWN")
        with self.assertRaises(ValueError):
            convert_minor(1, 1, 1, "ROUND_HALF_UP")


if __name__ == "__main__":
    unittest.main()
