"""Validation rules shared by the history audit and SQLite staging."""
from decimal import Decimal, InvalidOperation, localcontext

MARKET_NET_TOLERANCE_TWD = Decimal("10")


def market_net_difference(values):
    """Return (buy - sell) - the published net, in TWD."""
    try:
        buy, sell, net = (Decimal(values[key]) for key in ("buy_amount", "sell_amount", "net_amount"))
    except (KeyError, TypeError, InvalidOperation) as exc:
        raise ValueError("invalid_market_amount") from exc
    if not all(value.is_finite() for value in (buy, sell, net)):
        raise ValueError("invalid_market_amount")
    amounts = (buy, sell, net)
    whole_digits = max(1, *(value.adjusted() + 1 for value in amounts))
    fractional_digits = max(0, *(-value.as_tuple().exponent for value in amounts))
    with localcontext() as context:
        context.prec = whole_digits + fractional_digits + 2
        return buy - sell - net
