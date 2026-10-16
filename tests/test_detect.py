"""Detection tests against real pages saved from proshop.innovadiscs.com.

sold_out.html  - the target product (5086), captured 2026-10-15 while sold out.
in_stock.html  - another product from the same store (5003), captured in stock.
challenge.html - a Cloudflare managed-challenge interstitial.
malformed.html - the sold-out page truncated mid-document.
"""

from pathlib import Path

import pytest

from detect import StockState, detect

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_real_sold_out_page_is_out_of_stock():
    det = detect(load("sold_out.html"))
    assert det.state is StockState.OUT_OF_STOCK
    assert det.signals == {
        "bcdata_instock": False,
        "ld_instock": False,
        "sold_out_banner": True,
        "cart_visible": False,
    }
    assert det.name == "Duo Destroyer Calvin Heimburg (Commemorative)"
    assert det.price == "$20.99"
    assert det.product_id == "5086"


def test_real_in_stock_page_is_in_stock():
    det = detect(load("in_stock.html"))
    assert det.state is StockState.IN_STOCK
    assert det.signals["sold_out_banner"] is False
    assert det.signals["cart_visible"] is True


def test_sold_out_page_is_not_fooled_by_purchasable_true():
    # The live sold-out page has "purchasable": true; only "instock" matters.
    html = load("sold_out.html")
    assert '"purchasable":true' in html
    assert detect(html).state is StockState.OUT_OF_STOCK


def test_sold_out_page_has_enabled_add_to_cart_button_but_is_not_in_stock():
    # The button isn't disabled when sold out; only its wrapper is hidden.
    html = load("sold_out.html")
    assert 'id="form-action-addToCart"' in html
    assert detect(html).state is not StockState.IN_STOCK


@pytest.mark.parametrize("name", ["challenge.html", "malformed.html"])
def test_bad_pages_are_unknown(name):
    assert detect(load(name)).state is StockState.UNKNOWN


def test_challenge_page_is_flagged():
    assert detect(load("challenge.html")).signals.get("challenge") is True


@pytest.mark.parametrize("html", [None, "", "   ", "<html><body>Hello</body></html>", "not html at all {"])
def test_empty_or_unrelated_is_unknown(html):
    assert detect(html).state is StockState.UNKNOWN


def test_normal_page_cloudflare_precursor_script_is_not_a_challenge():
    assert "/cdn-cgi/challenge-platform/scripts/precursor" in load("sold_out.html")
    assert "challenge" not in detect(load("sold_out.html")).signals


# --- signal disagreement: each single flipped signal must yield UNKNOWN -------

def _sold_out_with(*replacements):
    html = load("sold_out.html")
    for old, new in replacements:
        assert old in html, old
        html = html.replace(old, new)
    return html


BCDATA_OUT = '"instock":false'
BCDATA_IN = '"instock":true'
WRAPPER_HIDDEN = 'class="add-to-cart-wrapper" style="display: none"'
WRAPPER_SHOWN = 'class="add-to-cart-wrapper"'
LD_OUT = '"https://schema.org/OutOfStock"'
LD_IN = '"https://schema.org/InStock"'
BANNER = '<span id="alertBox-message-text">Sold Out</span>'
NO_BANNER = ""


@pytest.mark.parametrize(
    "replacements",
    [
        pytest.param([(BCDATA_OUT, BCDATA_IN)], id="only-bcdata-says-in"),
        pytest.param([(WRAPPER_HIDDEN, WRAPPER_SHOWN)], id="cart-shown-with-banner"),
        pytest.param([(BCDATA_OUT, BCDATA_IN), (BANNER, NO_BANNER), (WRAPPER_HIDDEN, WRAPPER_SHOWN)],
                     id="all-in-except-json-ld"),
        pytest.param([(BCDATA_OUT, BCDATA_IN), (LD_OUT, LD_IN), (BANNER, NO_BANNER)],
                     id="cart-still-hidden"),
        pytest.param([(LD_OUT, LD_IN), (BANNER, NO_BANNER), (WRAPPER_HIDDEN, WRAPPER_SHOWN)],
                     id="bcdata-still-out"),
    ],
)
def test_any_disagreement_is_unknown(replacements):
    assert detect(_sold_out_with(*replacements)).state is StockState.UNKNOWN


def test_hidden_cart_without_banner_is_still_out_of_stock():
    # A hidden cart is itself an out-of-stock signal, so this agrees with BCData.
    assert detect(_sold_out_with((BANNER, NO_BANNER))).state is StockState.OUT_OF_STOCK


def test_synthetic_restock_of_target_page_is_in_stock():
    html = _sold_out_with(
        (BCDATA_OUT, BCDATA_IN), (LD_OUT, LD_IN), (BANNER, NO_BANNER), (WRAPPER_HIDDEN, WRAPPER_SHOWN)
    )
    assert detect(html).state is StockState.IN_STOCK


def test_disabled_button_is_not_in_stock():
    html = load("in_stock.html").replace('id="form-action-addToCart"', 'id="form-action-addToCart" disabled')
    assert detect(html).state is StockState.UNKNOWN


def test_missing_bcdata_is_unknown():
    html = load("in_stock.html").replace("var BCData", "var SomethingElse")
    assert detect(html).state is StockState.UNKNOWN


def test_json_endpoint_must_agree():
    html = load("in_stock.html")
    assert detect(html, json_attrs={"instock": True}).state is StockState.IN_STOCK
    assert detect(html, json_attrs={"instock": False}).state is StockState.UNKNOWN
    assert detect(load("sold_out.html"), json_attrs={"instock": False}).state is StockState.OUT_OF_STOCK
