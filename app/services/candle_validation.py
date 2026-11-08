"""Exact source-volume validation, distinct from connection/database outages."""
from decimal import Decimal, InvalidOperation

from app.services.capital_history_connection import CapitalHistoryError


MAX_CANDLE_VOLUME = 2**64 - 1
DATA_FAILURE_LIMIT = 5
DATA_RETRY_SECONDS = 15


class CandleDataError(CapitalHistoryError):
    def __init__(self, message, *, details=None):
        super().__init__(message)
        self.details = dict(details or {})


def find_candle_data_error(error):
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if isinstance(error, CandleDataError):
            return error
        error = error.__cause__ or error.__context__
    return None


def invalid_volume(value, timestamp=None, raw_record=None):
    details = dict(field="volume", raw_value=str(value)[:256],
                   timestamp=timestamp.isoformat() if timestamp is not None else None)
    if raw_record is not None:
        details["raw_record"] = str(raw_record)[:1024]
    return CandleDataError(
        f"成交量資料無效：{details['timestamp'] or '未知日期'} volume={details['raw_value']}；"
        f"必須為 0～{MAX_CANDLE_VOLUME} 的整數，未寫入本區間", details=details)


def parse_volume(raw, timestamp=None, raw_record=None):
    """No float conversion/truncation, including UINT64_MAX and >2**53."""
    try:
        value = Decimal(str(raw).replace(",", "").strip())
        if (not value.is_finite() or not 0 <= value <= MAX_CANDLE_VOLUME
                or value != value.to_integral_value()):
            raise InvalidOperation
        return int(value)
    except (InvalidOperation, ValueError, TypeError, OverflowError):
        raise invalid_volume(raw, timestamp, raw_record) from None


def validate_candle_volumes(candles):
    for index, candle in enumerate(candles, 1):
        value = candle.volume
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_CANDLE_VOLUME:
            error = invalid_volume(value, candle.timestamp)
            error.details["batch_row"] = index
            raise error
