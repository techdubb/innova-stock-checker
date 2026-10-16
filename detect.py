"""Pure stock detection for a BigCommerce Stencil product page.

`detect()` takes page HTML (and optionally the JSON from
/remote/v1/product-attributes/<id>) and returns a Detection. It does no I/O.

Signals observed on the live Innova store (2026-10-15):

  * BCData:  inline `var BCData = {"product_attributes": {... "instock": false ...}}`.
             NOTE: `purchasable` stays true while sold out, so only `instock` is used.
  * JSON-LD: `"availability": "https://schema.org/OutOfStock"` / `InStock`.
  * Banner:  `<span id="alertBox-message-text">Sold Out</span>` (absent when in stock).
  * Cart:    `<div id="add-to-cart-wrapper" style="display: none">` when sold out.
             The Add to Cart <input> itself is present and NOT disabled either way,
             so the wrapper's visibility is what matters.

IN_STOCK requires every available signal to agree. Anything ambiguous is UNKNOWN.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from bs4 import BeautifulSoup


class StockState(str, Enum):
    IN_STOCK = "IN_STOCK"
    OUT_OF_STOCK = "OUT_OF_STOCK"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class Detection:
    state: StockState
    reason: str
    signals: dict[str, Any] = field(default_factory=dict)
    name: str | None = None
    price: str | None = None
    product_id: str | None = None


def unknown(reason: str, **signals: Any) -> Detection:
    return Detection(StockState.UNKNOWN, reason, dict(signals))


# Markers of a Cloudflare (or similar) interstitial. Deliberately excludes
# "/cdn-cgi/challenge-platform/scripts/precursor", which normal pages include.
_CHALLENGE_MARKERS = (
    "_cf_chl_opt",
    "cf-chl-",
    "<title>just a moment...</title>",
    "attention required! | cloudflare",
    "verifying you are human",
    "enable javascript and cookies to continue",
)

_BCDATA_RE = re.compile(r"var\s+BCData\s*=\s*")
_LD_AVAILABILITY_RE = re.compile(r'"availability"\s*:\s*"https?://schema\.org/(\w+)"')
_PRODUCT_ID_RE = re.compile(r'name="product_id"\s+value="(\d+)"')


def is_challenge_page(html: str) -> bool:
    lowered = html.lower()
    return any(marker in lowered for marker in _CHALLENGE_MARKERS)


def _parse_bcdata(html: str) -> dict[str, Any] | None:
    match = _BCDATA_RE.search(html)
    if not match:
        return None
    try:
        data, _ = json.JSONDecoder().raw_decode(html, match.end())
    except ValueError:
        return None
    attrs = data.get("product_attributes") if isinstance(data, dict) else None
    return attrs if isinstance(attrs, dict) else None


def _instock_flag(attrs: dict[str, Any] | None) -> bool | None:
    if not attrs:
        return None
    value = attrs.get("instock")
    return value if isinstance(value, bool) else None


def _ld_availability(html: str) -> bool | None:
    match = _LD_AVAILABILITY_RE.search(html)
    if not match:
        return None
    value = match.group(1)
    if value in ("InStock", "LimitedAvailability", "OnlineOnly"):
        return True
    if value in ("OutOfStock", "SoldOut", "Discontinued", "PreOrder", "BackOrder"):
        return False
    return None


def _is_hidden(tag) -> bool:
    style = (tag.get("style") or "").replace(" ", "").lower()
    return "display:none" in style or "visibility:hidden" in style or tag.has_attr("hidden")


def _cart_visible(soup: BeautifulSoup) -> bool | None:
    """True if the add-to-cart wrapper and an enabled button are visible.

    None if the expected markup isn't there at all (layout change -> UNKNOWN).
    """
    wrapper = soup.find(id="add-to-cart-wrapper")
    button = soup.find(id="form-action-addToCart")
    if wrapper is None or button is None:
        return None
    if _is_hidden(wrapper):
        return False
    if button.has_attr("disabled") or _is_hidden(button):
        return False
    return True


def _sold_out_banner(soup: BeautifulSoup) -> bool:
    banner = soup.find(id="alertBox-message-text")
    if banner is None:
        return False
    text = banner.get_text(" ", strip=True).lower()
    return "sold out" in text or "out of stock" in text


def _product_name(soup: BeautifulSoup) -> str | None:
    h1 = soup.select_one("h1.productView-title")
    return h1.get_text(strip=True) if h1 else None


def _price(attrs: dict[str, Any] | None) -> str | None:
    try:
        return attrs["price"]["without_tax"]["formatted"]  # type: ignore[index]
    except (KeyError, TypeError):
        return None


def detect(html: str | None, json_attrs: dict[str, Any] | None = None) -> Detection:
    """Classify a product page. Never raises; never returns IN_STOCK on ambiguity.

    `json_attrs` is the `data` object from /remote/v1/product-attributes/<id>,
    if the caller fetched it. When given, it must agree with the inline BCData.
    """
    try:
        return _detect(html, json_attrs)
    except Exception as exc:  # parser bugs must never become false alerts
        return unknown(f"parse error: {type(exc).__name__}: {exc}")


def _detect(html: str | None, json_attrs: dict[str, Any] | None) -> Detection:
    if not html or not html.strip():
        return unknown("empty body")
    if is_challenge_page(html):
        return unknown("challenge page", challenge=True)

    soup = BeautifulSoup(html, "html.parser")
    bc_attrs = _parse_bcdata(html)
    signals: dict[str, Any] = {
        "bcdata_instock": _instock_flag(bc_attrs),
        "ld_instock": _ld_availability(html),
        "sold_out_banner": _sold_out_banner(soup),
        "cart_visible": _cart_visible(soup),
    }
    if json_attrs is not None:
        signals["json_instock"] = _instock_flag(json_attrs)

    product_id_match = _PRODUCT_ID_RE.search(html)
    meta = {
        "name": _product_name(soup),
        "price": _price(bc_attrs) or _price(json_attrs),
        "product_id": product_id_match.group(1) if product_id_match else None,
    }

    def result(state: StockState, reason: str) -> Detection:
        return Detection(state, reason, signals, **meta)

    # Availability data: inline BCData, cross-checked against the JSON endpoint if given.
    data_flags = [signals[k] for k in ("bcdata_instock", "json_instock") if signals.get(k) is not None]
    if not data_flags:
        return result(StockState.UNKNOWN, "no availability data (BCData missing)")
    if len(set(data_flags)) > 1:
        return result(StockState.UNKNOWN, "BCData and JSON endpoint disagree")
    data_instock = data_flags[0]

    # HTML: in stock only when the banner is absent AND the cart is visible.
    if signals["cart_visible"] is None:
        return result(StockState.UNKNOWN, "add-to-cart markup not found")
    if signals["sold_out_banner"] and signals["cart_visible"]:
        return result(StockState.UNKNOWN, "sold-out banner shown with visible cart")
    html_instock = signals["cart_visible"] and not signals["sold_out_banner"]

    if data_instock != html_instock:
        return result(StockState.UNKNOWN, "availability data and HTML disagree")
    ld = signals["ld_instock"]
    if ld is not None and ld != data_instock:
        return result(StockState.UNKNOWN, "JSON-LD availability disagrees")

    if data_instock:
        return result(StockState.IN_STOCK, "all signals in stock")
    return result(StockState.OUT_OF_STOCK, "all signals out of stock")
