from .base import BaseIndicator
from .supertrend import SupertrendIndicator
from .ut_bot import UTBotIndicator

INDICATORS: dict[str, type[BaseIndicator]] = {
    UTBotIndicator.name: UTBotIndicator,
    SupertrendIndicator.name: SupertrendIndicator,
}


def get_indicator(name: str) -> type[BaseIndicator]:
    try:
        return INDICATORS[name]
    except KeyError:
        raise ValueError(f"Unknown indicator '{name}'. Available: {', '.join(INDICATORS)}") from None
