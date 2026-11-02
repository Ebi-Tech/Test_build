"""Per-shop token check: a shared secret from the config file, nothing fancy."""

from __future__ import annotations

import hmac

from . import config


def authenticate(shop_id: str, token: str) -> dict | None:
    """Return the shop config when the token matches, otherwise None."""
    if not shop_id or not token:
        return None
    shop = config.shop(shop_id)
    if not shop:
        return None
    expected = str(shop.get("token", ""))
    if not expected:
        return None
    if hmac.compare_digest(expected, str(token)):
        return shop
    return None
