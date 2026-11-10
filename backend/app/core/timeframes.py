TIMEFRAMES: dict[str, int] = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240, "1d": 1440}


def interval_minutes(timeframe: str) -> int:
    try:
        return TIMEFRAMES[timeframe]
    except KeyError:
        raise ValueError(f"Unsupported timeframe '{timeframe}'. Supported: {', '.join(TIMEFRAMES)}") from None


def interval_ms(timeframe: str) -> int:
    return interval_minutes(timeframe) * 60_000
