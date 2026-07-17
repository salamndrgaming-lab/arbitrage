"""Live autonomous trading layer.

Everything in this package places or manages REAL orders with REAL funds.
It is inert unless explicitly armed — see ``arb.trading.trader.arm_check``:
config ``trading.enabled: true`` AND the environment variable
``ARB_TRADING_ARMED=I-ACCEPT-THE-RISK`` AND per-venue API credentials must
all be present, otherwise nothing here will start.
"""

ARMING_PHRASE = "I-ACCEPT-THE-RISK"
ARMING_ENV = "ARB_TRADING_ARMED"
