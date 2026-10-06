"""Deterministic job normalization, scoring, and resume-ranking helpers."""

from __future__ import annotations


DEFAULT_FIT_SCORE_WEIGHTS = {
    "role_title": 20,
    "required_skills": 30,
    "backend_api": 15,
    "experience_seniority": 10,
    "location_workplace": 10,
    "ai": 5,
    "database_data": 5,
    "cloud_infrastructure": 5,
}


def _score(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 100:
        raise ValueError(f"{name} must be between 0 and 100")
    return value


def enabled_role_profiles(config: dict) -> list[dict]:
    """Return enabled target roles from an already-normalized config."""
    return [profile for profile in config.get("target_roles", []) if profile.get("enabled", True)]


def normalize_config(raw: dict) -> dict:
    """Add new defaults while accepting the repository's legacy config shape."""
    if not isinstance(raw, dict):
        raise ValueError("config must be a JSON object")
    config = dict(raw)
    minimum = _score(config.get("minimum_fit_score", 70), "minimum_fit_score")
    stretch = _score(config.get("stretch_fit_score", 60), "stretch_fit_score")
    config["minimum_fit_score"] = minimum
    config["stretch_fit_score"] = stretch

    if "posted_within_days" in config:
        days = config["posted_within_days"]
    else:
        days = 1 if config.get("past_24_hours") is True else 7
    if isinstance(days, bool) or days not in (1, 3, 7):
        raise ValueError("posted_within_days must be 1, 3, or 7")
    config["posted_within_days"] = days

    weights = config.get("fit_score_weights", DEFAULT_FIT_SCORE_WEIGHTS)
    if not isinstance(weights, dict) or any(
        isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0
        for value in weights.values()
    ):
        raise ValueError("fit_score_weights must contain non-negative numbers")
    if set(weights) != set(DEFAULT_FIT_SCORE_WEIGHTS) or sum(weights.values()) != 100:
        raise ValueError("fit_score_weights must contain every component and total 100")
    config["fit_score_weights"] = dict(weights)

    profiles = config.get("target_roles")
    if not isinstance(profiles, list) or not profiles:
        legacy_roles = config.get("roles")
        names = legacy_roles if isinstance(legacy_roles, list) and legacy_roles else [config.get("role", "Software Engineer")]
        profiles = [{"name": name} for name in names]

    normalized = []
    for raw_profile in profiles:
        if not isinstance(raw_profile, dict):
            raise ValueError("each target role must be an object")
        profile = dict(raw_profile)
        profile.setdefault("enabled", True)
        if not profile["enabled"]:
            normalized.append(profile)
            continue
        name = profile.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("target role name must be a non-empty string")
        priority = profile.get("priority", 1)
        if isinstance(priority, bool) or priority not in (1, 2, 3):
            raise ValueError("target role priority must be 1, 2, or 3")
        keywords = profile.get("keywords", [])
        if not isinstance(keywords, list) or not all(isinstance(item, str) for item in keywords):
            raise ValueError("target role keywords must be a list of strings")
        profile_minimum = _score(
            profile.get("minimum_fit_score", minimum),
            "target role minimum_fit_score",
        )
        normalized.append(
            {
                **profile,
                "name": name.strip(),
                "priority": priority,
                "keywords": [item.strip() for item in keywords if item.strip()],
                "minimum_fit_score": profile_minimum,
            }
        )
    config["target_roles"] = normalized
    config.setdefault("locations", ["Karachi", "Pakistan"])
    config.setdefault("remote", True)
    config.setdefault("preferred_companies", [])
    config.setdefault("excluded_companies", [])
    return config


def recency_filter(days: int) -> str:
    try:
        return {1: "r86400", 3: "r259200", 7: "r604800"}[days]
    except KeyError as exc:
        raise ValueError("posted_within_days must be 1, 3, or 7") from exc
