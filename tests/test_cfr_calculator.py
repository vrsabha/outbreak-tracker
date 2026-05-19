"""Tests for CFR calculations."""

from __future__ import annotations

import pandas as pd

from transform.cfr_calculator import add_cfr_columns, wilson_interval


def test_wilson_interval_returns_bounds_for_realistic_outbreak() -> None:
    """Wilson interval should be bounded and contain the observed CFR."""
    lower, upper = wilson_interval(deaths=151, cases=284)

    assert lower is not None
    assert upper is not None
    assert 0 < lower < 151 / 284 < upper < 1


def test_add_cfr_columns_handles_zero_and_small_counts() -> None:
    """CFR enrichment should not divide by zero and should flag tiny samples."""
    df = pd.DataFrame(
        [
            {"confirmed_cases": 0, "deaths": 0},
            {"confirmed_cases": 9, "deaths": 2},
            {"confirmed_cases": 100, "deaths": 40},
        ]
    )

    output = add_cfr_columns(df)

    assert pd.isna(output.loc[0, "cfr"])
    assert output.loc[1, "statistically_unreliable"]
    assert not output.loc[2, "statistically_unreliable"]
    assert output.loc[2, "cfr_ci_lower"] < output.loc[2, "cfr"] < output.loc[2, "cfr_ci_upper"]

