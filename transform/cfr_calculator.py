"""Case fatality rate calculations with Wilson confidence intervals."""

from __future__ import annotations

import logging
from typing import Optional, Tuple

import pandas as pd
from scipy import stats

LOGGER = logging.getLogger(__name__)


def wilson_interval(
    deaths: Optional[float],
    cases: Optional[float],
    confidence: float = 0.95,
) -> Tuple[Optional[float], Optional[float]]:
    """Calculate a Wilson score confidence interval for a binomial proportion.

    Args:
        deaths: Number of fatal outcomes.
        cases: Number of confirmed cases.
        confidence: Confidence level for the interval.

    Returns:
        A tuple of lower and upper confidence bounds. Bounds are ``None`` when
        the denominator is missing or zero.
    """
    if cases is None or pd.isna(cases) or cases <= 0:
        return None, None
    if deaths is None or pd.isna(deaths):
        deaths = 0

    bounded_deaths = min(max(float(deaths), 0.0), float(cases))
    proportion = bounded_deaths / float(cases)
    z_score = stats.norm.ppf(1 - (1 - confidence) / 2)
    denominator = 1 + (z_score**2 / cases)
    centre = proportion + (z_score**2 / (2 * cases))
    margin = z_score * (
        (proportion * (1 - proportion) / cases) + (z_score**2 / (4 * cases**2))
    ) ** 0.5

    lower = max(0.0, (centre - margin) / denominator)
    upper = min(1.0, (centre + margin) / denominator)
    return lower, upper


def add_cfr_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Add CFR, confidence bounds, and reliability flags to an outbreak dataset.

    Args:
        df: Outbreak records containing ``confirmed_cases`` and ``deaths``.

    Returns:
        A copy of ``df`` with CFR fields appended.
    """
    output = df.copy()
    for column in ("confirmed_cases", "deaths"):
        if column not in output.columns:
            output[column] = pd.NA
        output[column] = pd.to_numeric(output[column], errors="coerce")

    output["cfr"] = output.apply(
        lambda row: (
            row["deaths"] / row["confirmed_cases"]
            if pd.notna(row["confirmed_cases"]) and row["confirmed_cases"] > 0
            else pd.NA
        ),
        axis=1,
    )
    bounds = output.apply(
        lambda row: wilson_interval(row["deaths"], row["confirmed_cases"]),
        axis=1,
        result_type="expand",
    )
    output["cfr_ci_lower"] = bounds[0]
    output["cfr_ci_upper"] = bounds[1]
    output["statistically_unreliable"] = output["confirmed_cases"].fillna(0) < 10
    LOGGER.info("Calculated CFR fields for %s outbreak records", len(output))
    return output

