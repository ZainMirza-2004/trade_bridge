from decimal import ROUND_DOWN, Decimal, InvalidOperation

ZERO = Decimal("0")


def to_decimal(value, default: Decimal | None = None) -> Decimal | None:
    if value is None or value == "":
        return default
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return default


def round_down(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return value
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def fmt(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return format(value.normalize(), "f") if value != 0 else "0"
