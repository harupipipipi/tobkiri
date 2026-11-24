"""Canonical WIT-width scientific strings for finite guest float values."""

import math
import struct
from _pydecimal import Decimal, localcontext, ROUND_HALF_UP


def to_canon_float(value: float, width: str) -> str:
    """Produce shortest scientific strings, with f32 rounding and signed zero."""
    if width not in {"f32", "f64"}:
        raise ValueError("float width must be f32 or f64")
    if not math.isfinite(value):
        raise ValueError("non-finite WIT float")
    if width == "f32":
        try:
            value = struct.unpack("!f", struct.pack("!f", value))[0]
        except OverflowError:
            raise ValueError("WIT f32 overflow") from None
    if not math.isfinite(value):
        raise ValueError("non-finite WIT float")
    if value == 0:
        return "-0e0" if math.copysign(1, value) < 0 else "0e0"
    decimal = repr(value)
    if width == "f32":
        for precision in range(1, 10):
            # Rust/ECMAScript shortest formatting chooses the larger magnitude
            # at an exact decimal tie; Python's default formatter uses even.
            with localcontext() as context:
                context.rounding = ROUND_HALF_UP
                candidate = format(Decimal.from_float(value), f".{precision}g")
            if struct.pack("!f", float(candidate)) == struct.pack("!f", value):
                decimal = candidate
                break
    negative = decimal.startswith("-")
    decimal = decimal.lstrip("-").lower()
    mantissa, _, exponent = decimal.partition("e")
    integer_digits = mantissa.find(".") if "." in mantissa else len(mantissa)
    digits = mantissa.replace(".", "")
    leading = len(digits) - len(digits.lstrip("0"))
    power = int(exponent or "0") + integer_digits - leading - 1
    digits = digits[leading:].rstrip("0")
    normalized = digits[0] + ("." + digits[1:] if len(digits) > 1 else "")
    return ("-" if negative else "") + normalized + "e" + str(power)
