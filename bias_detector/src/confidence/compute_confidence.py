def confidence(score: float, quality_flags: dict) -> dict:
    """
    Returns a dict:
    {
        "pct": int,
        "breakdown": ["missing_feature:10%", "stale_calendar:5%"]
    }
    """
    base = min(100, int(abs(score) * 100))
    haircut = 0
    breakdown = []

    if quality_flags.get("missing_features"):
        haircut += 10
        breakdown.append("missing_features:10%")
    if quality_flags.get("stale_calendar"):
        haircut += 5
        breakdown.append("stale_calendar:5%")

    pct = max(0, base - haircut)
    return {"pct": pct, "breakdown": breakdown}
