"""The batch prefilter's stage split — `_house_number` / `_house_number_pattern`
(decision-batch-geocoding-county-scale.md F2 / F2a).

Pure functions, no DB. These pin the rule that decides which inputs get the cheap, exact house-number
prefix (stage A) and which fall back to the wide `%` similarity net (stage B). Two properties are
load-bearing: the token must be a BARE leading number (a `1415-B` or `12A` token falls to stage B, where
fuzzy matching can still find it), and the pattern must end in a space so `7501 X ST` can never survive
the prefilter for `750 X ST`.
"""
import pytest

from app.geocode_internal import _house_number, _house_number_pattern, _normalize_q


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("750 Example Way Springfield ST", "750"),
        ("1234 5TH STREET CIR E. SPRINGFIELD ST", "1234"),
        ("  12 A St", "12"),            # leading whitespace is normalized away first
        ("100\tMaple Dr", "100"),  # a tab counts as the separator after normalization
        ("200 3rd", "200"),          # F1a's messy row still keys on its number
        ("1415-B Main St", None),       # not a bare number → stage B
        ("12A Main St", None),          # alpha-suffixed number → stage B
        ("PO BOX 894 Springfield ST", None),
        ("One Main St", None),
        ("Main St & 1st Ave", None),
        ("12345", None),                # a bare number with nothing after it is not an address
        ("", None),
    ],
)
def test_house_number_detection(raw: str, expected: str | None) -> None:
    assert _house_number(_normalize_q(raw)) == expected


def test_pattern_keeps_the_trailing_space() -> None:
    # `'750 %'`, never `'750%'`: the space is what keeps 7501 out of 750's candidate set.
    assert _house_number_pattern("750") == "750 %"
    assert _house_number_pattern("12") == "12 %"


def test_pattern_escapes_like_metacharacters() -> None:
    # Digits-only by construction, but the helper must not rely on that.
    assert _house_number_pattern("7%5_0") == r"7\%5\_0 %"
