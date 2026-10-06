"""Deterministic job normalization, scoring, and resume-ranking helpers."""

from __future__ import annotations

import re
import urllib.parse
from datetime import date, timedelta

from gemini_client import GeminiError


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


JOB_FIELDS = {
    "job_id": None,
    "url": "",
    "title": "",
    "company": "",
    "location": "",
    "employment_type": None,
    "workplace_type": None,
    "seniority": None,
    "posting_date": None,
    "applicant_count": None,
    "description": "",
    "required_skills": [],
    "preferred_skills": [],
    "years_experience": None,
    "education_requirement": None,
    "responsibilities": [],
    "application_method": None,
    "matched_roles": [],
}
LIST_JOB_FIELDS = {
    "required_skills",
    "preferred_skills",
    "responsibilities",
    "matched_roles",
}


def _linkedin_job_id(url: str) -> str | None:
    parsed = urllib.parse.urlsplit(url or "")
    query = urllib.parse.parse_qs(parsed.query)
    identifier = (query.get("currentJobId") or [None])[0]
    if identifier:
        return str(identifier)
    match = re.search(r"/jobs/view/(?:[^/?#]*-)?(\d+)(?:/|$)", parsed.path)
    return match.group(1) if match else None


def canonical_job_url(url: str, job_id: str | None = None) -> str:
    identifier = str(job_id) if job_id else _linkedin_job_id(url)
    if identifier:
        return f"https://www.linkedin.com/jobs/view/{identifier}"
    parsed = urllib.parse.urlsplit((url or "").strip())
    if not parsed.scheme or not parsed.netloc:
        return (url or "").strip()
    path = parsed.path.rstrip("/") or "/"
    return urllib.parse.urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), path, "", ""))


def _posting_date(value, today: date) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return text
    if re.search(r"\b(today|hour|minute|just now)\b", text, re.I):
        return today.isoformat()
    match = re.search(r"(\d+)\s+(day|week)s?\s+ago", text, re.I)
    if match:
        count = int(match.group(1)) * (7 if match.group(2).lower() == "week" else 1)
        return (today - timedelta(days=count)).isoformat()
    return text or None


def normalize_job(raw: dict, matched_role: str, today: date | None = None) -> dict:
    """Return the stable JSON-compatible job shape used by later pipeline stages."""
    raw = raw if isinstance(raw, dict) else {}
    result = {
        key: list(default) if isinstance(default, list) else default
        for key, default in JOB_FIELDS.items()
    }
    for key in result:
        if key not in raw or raw[key] is None:
            continue
        if key in LIST_JOB_FIELDS:
            value = raw[key] if isinstance(raw[key], list) else []
            result[key] = list(dict.fromkeys(item for item in value if item))
        elif isinstance(result[key], str):
            result[key] = str(raw[key]).strip()
        else:
            result[key] = raw[key]

    identifier = raw.get("job_id") or _linkedin_job_id(raw.get("url", ""))
    result["job_id"] = str(identifier) if identifier else None
    result["url"] = canonical_job_url(raw.get("url", ""), result["job_id"])
    result["posting_date"] = _posting_date(raw.get("posting_date"), today or date.today())
    result["matched_roles"] = list(
        dict.fromkeys(result["matched_roles"] + ([matched_role] if matched_role else []))
    )
    return result


def _identity_text(value) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", str(value or "").lower())).strip()


def job_key(job: dict) -> str:
    if job.get("job_id"):
        return f"linkedin:{job['job_id']}"
    if job.get("url"):
        return f"url:{canonical_job_url(job['url'])}"
    details = "|".join(
        _identity_text(job.get(field)) for field in ("company", "title", "location")
    )
    return f"details:{details}"


def merge_jobs(existing: dict, incoming: dict) -> dict:
    """Merge two observations of the same job without discarding richer data."""
    merged = dict(existing)
    for key in JOB_FIELDS:
        old = merged.get(key)
        new = incoming.get(key)
        if key in LIST_JOB_FIELDS:
            merged[key] = list(dict.fromkeys((old or []) + (new or [])))
        elif key == "applicant_count" and old is not None and new is not None:
            merged[key] = min(old, new)
        elif not old or (isinstance(old, str) and isinstance(new, str) and len(new) > len(old)):
            merged[key] = new
    return merged


def deduplicate_jobs(jobs: list[dict]) -> list[dict]:
    unique = {}
    for job in jobs:
        key = job_key(job)
        unique[key] = merge_jobs(unique[key], job) if key in unique else job
    return list(unique.values())


SKILL_ALIASES = {
    "Python": ("python",),
    "Django": ("django",),
    "FastAPI": ("fastapi", "fast api"),
    "Laravel": ("laravel",),
    "PHP": ("php",),
    "JavaScript": ("javascript",),
    "TypeScript": ("typescript",),
    "React": ("react", "react.js", "reactjs"),
    "Node.js": ("node.js", "nodejs"),
    "PostgreSQL": ("postgresql", "postgres"),
    "MySQL": ("mysql",),
    "MongoDB": ("mongodb",),
    "SQL": ("sql",),
    "Docker": ("docker",),
    "Kubernetes": ("kubernetes", "k8s"),
    "AWS": ("aws", "amazon web services"),
    "Azure": ("azure",),
    "GCP": ("gcp", "google cloud"),
    "REST APIs": ("rest api", "restful api"),
    "GraphQL": ("graphql",),
    "Airflow": ("airflow",),
    "Spark": ("spark",),
    "LangChain": ("langchain",),
    "LangGraph": ("langgraph",),
    "RAG": ("retrieval augmented generation", "rag"),
}

JOB_ANALYSIS_KEYS = {
    "required_skills",
    "preferred_skills",
    "years_experience",
    "education_requirement",
    "responsibilities",
    "employment_type",
    "workplace_type",
    "seniority",
}


def _skills_in(text: str) -> list[str]:
    lowered = text.lower()
    found = []
    for canonical, aliases in SKILL_ALIASES.items():
        if any(re.search(rf"(?<!\w){re.escape(alias)}(?!\w)", lowered) for alias in aliases):
            found.append(canonical)
    return found


def deterministic_job_analysis(job: dict) -> dict:
    description = job.get("description") or ""
    required = list(job.get("required_skills") or [])
    preferred = list(job.get("preferred_skills") or [])
    responsibilities = list(job.get("responsibilities") or [])
    mode = "required"
    for raw_line in description.splitlines():
        line = raw_line.strip().lstrip("•-* ")
        if not line:
            continue
        lowered = line.lower()
        if lowered.startswith(("preferred", "nice to have", "bonus")):
            mode = "preferred"
        elif lowered.startswith(("responsibilities", "what you'll do", "what you will do")):
            mode = "responsibilities"
            line = line.split(":", 1)[1].strip() if ":" in line else ""
        elif lowered.startswith(("requirements", "required", "qualifications")):
            mode = "required"
        if line:
            skills = _skills_in(line)
            if mode == "preferred":
                preferred.extend(skills)
            elif mode == "required":
                required.extend(skills)
            elif re.match(r"(?i)^(build|develop|design|implement|maintain|create|collaborate|lead)\b", line):
                responsibilities.append(line)

    years = job.get("years_experience")
    if years is None:
        match = re.search(r"(\d+(?:\.\d+)?)\+?\s*(?:years?|yrs?)\b", description, re.I)
        years = float(match.group(1)) if match and "." in match.group(1) else int(match.group(1)) if match else None

    education = job.get("education_requirement")
    if education is None:
        if re.search(r"bachelor(?:'s)?\s+degree", description, re.I):
            education = "Bachelor's degree"
        elif re.search(r"master(?:'s)?\s+degree", description, re.I):
            education = "Master's degree"

    title_text = f"{job.get('title') or ''} {job.get('seniority') or ''}".lower()
    seniority = job.get("seniority")
    if not seniority:
        if "senior" in title_text or "lead" in title_text:
            seniority = "Senior"
        elif "junior" in title_text or "entry" in title_text:
            seniority = "Entry level"
        elif "intern" in title_text:
            seniority = "Internship"

    workplace = job.get("workplace_type")
    if not workplace and "remote" in (job.get("location") or "").lower():
        workplace = "Remote"

    return {
        "required_skills": list(dict.fromkeys(required)),
        "preferred_skills": list(dict.fromkeys(preferred)),
        "years_experience": years,
        "education_requirement": education,
        "responsibilities": list(dict.fromkeys(responsibilities)),
        "employment_type": job.get("employment_type"),
        "workplace_type": workplace,
        "seniority": seniority,
    }


def validate_job_analysis(value: object) -> dict:
    if not isinstance(value, dict):
        raise ValueError("AI analysis must be a JSON object")
    unexpected = set(value) - JOB_ANALYSIS_KEYS
    if unexpected:
        raise ValueError(f"AI analysis contains unexpected fields: {sorted(unexpected)}")
    result = {}
    for key in ("required_skills", "preferred_skills", "responsibilities"):
        item = value.get(key, [])
        if not isinstance(item, list) or not all(isinstance(entry, str) for entry in item):
            raise ValueError(f"AI analysis field {key} must be a list of strings")
        result[key] = list(dict.fromkeys(entry.strip() for entry in item if entry.strip()))
    years = value.get("years_experience")
    if years is not None and (isinstance(years, bool) or not isinstance(years, (int, float)) or years < 0):
        raise ValueError("AI analysis years_experience must be a non-negative number or null")
    result["years_experience"] = years
    for key in ("education_requirement", "employment_type", "workplace_type", "seniority"):
        item = value.get(key)
        if item is not None and not isinstance(item, str):
            raise ValueError(f"AI analysis field {key} must be a string or null")
        result[key] = item.strip() if isinstance(item, str) and item.strip() else None
    return result


JOB_ANALYSIS_SYSTEM = (
    "Extract only requirements explicitly supported by the supplied job description. "
    "Return a JSON object using only the requested fields. Use null or empty lists when unknown."
)


def analyze_job(job: dict, gemini=None) -> dict:
    deterministic = deterministic_job_analysis(job)
    result = {**job, **deterministic, "analysis_concerns": []}
    incomplete = any(
        not deterministic.get(key)
        for key in (
            "required_skills",
            "responsibilities",
            "years_experience",
            "education_requirement",
        )
    )
    if not gemini or not incomplete:
        return result

    prompt = (
        "Analyze this job and return exactly these fields: required_skills, "
        "preferred_skills, years_experience, education_requirement, responsibilities, "
        "employment_type, workplace_type, seniority.\n\n"
        f"{job.get('title', '')}\n{job.get('location', '')}\n{job.get('description', '')}"
    )
    try:
        semantic = validate_job_analysis(
            gemini.generate_json_object(prompt, system=JOB_ANALYSIS_SYSTEM)
        )
    except (GeminiError, TypeError, ValueError) as exc:
        result["analysis_concerns"].append(f"AI analysis rejected: {exc}")
        return result

    for key in ("required_skills", "preferred_skills", "responsibilities"):
        result[key] = list(dict.fromkeys((result.get(key) or []) + semantic[key]))
    for key in ("years_experience", "education_requirement", "employment_type", "workplace_type", "seniority"):
        if not result.get(key) and semantic.get(key) is not None:
            result[key] = semantic[key]
    return result
