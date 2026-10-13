"""The IBKR tools run only when DESK_VENUE=ibkr. Any other value leaves them off."""

import os

OFF = "DESK_VENUE is not ibkr, so this tool stays off."


def enabled(env=None) -> bool:
    source = os.environ if env is None else env
    return str(source.get("DESK_VENUE", "")).strip().lower() == "ibkr"
