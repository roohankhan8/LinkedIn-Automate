"""Deterministic job normalization, scoring, and resume-ranking helpers."""

from __future__ import annotations

import re
import urllib.parse
import json
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


def canonical_skill(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9+#]+", " ", str(value or "").lower()).strip()
    normalized = re.sub(r"\s+", " ", normalized)
    for canonical, aliases in SKILL_ALIASES.items():
        candidates = (canonical, *aliases)
        if normalized in {
            re.sub(r"\s+", " ", re.sub(r"[^a-z0-9+#]+", " ", item.lower())).strip()
            for item in candidates
        }:
            return {
                "REST APIs": "rest api",
                "PostgreSQL": "postgresql",
                "Node.js": "node.js",
            }.get(canonical, canonical.lower())
    return normalized


RELATED_SKILL_GROUPS = [
    {"ai integration", "langchain", "langgraph", "rag"},
    {"django", "fastapi", "laravel", "backend"},
    {"postgresql", "mysql", "mongodb", "sql", "database"},
    {"aws", "azure", "gcp", "cloud"},
    {"react", "javascript", "typescript", "frontend"},
]


def _related_skill(required: str, available: set[str]) -> bool:
    return any(required in group and available.intersection(group) for group in RELATED_SKILL_GROUPS)


def _role_tokens(values) -> set[str]:
    ignored = {"engineer", "developer", "software", "full", "stack", "application"}
    text = " ".join(str(value) for value in values if value)
    return {
        token
        for token in re.findall(r"[a-z0-9+#]+", text.lower())
        if len(token) > 2 and token not in ignored
    }


def rank_resumes(job: dict, resume_profiles: dict[str, dict]) -> list[dict]:
    """Rank validated resume profiles against one analyzed job."""
    results = []
    required = job.get("required_skills") or []
    preferred = job.get("preferred_skills") or []
    job_role_tokens = _role_tokens([job.get("title"), *(job.get("matched_roles") or [])])
    job_text = " ".join(
        [
            job.get("title") or "",
            job.get("description") or "",
            *(job.get("responsibilities") or []),
        ]
    ).lower()

    for path, profile in resume_profiles.items():
        skills = list(profile.get("skills") or []) + list((profile.get("skill_years") or {}).keys())
        available = {canonical_skill(skill) for skill in skills}
        strong, partial, missing_required, missing_preferred = [], [], [], []

        required_credit = 0.0
        for skill in required:
            canonical = canonical_skill(skill)
            if canonical in available:
                strong.append(skill)
                required_credit += 1
            elif _related_skill(canonical, available):
                partial.append(skill)
                required_credit += 0.5
            else:
                missing_required.append(skill)

        preferred_credit = 0.0
        for skill in preferred:
            canonical = canonical_skill(skill)
            if canonical in available:
                strong.append(skill)
                preferred_credit += 1
            elif _related_skill(canonical, available):
                partial.append(skill)
                preferred_credit += 0.5
            else:
                missing_preferred.append(skill)

        required_points = 60 * required_credit / len(required) if required else 0
        preferred_points = 15 * preferred_credit / len(preferred) if preferred else 0
        profile_role_tokens = _role_tokens(
            [
                profile.get("headline"),
                *(profile.get("roles") or []),
                *(profile.get("target_role_categories") or []),
            ]
        )
        role_points = 15 if job_role_tokens.intersection(profile_role_tokens) else 0
        evidence = " ".join(
            [
                *(str(item) for item in (profile.get("domains") or [])),
                *(str(item) for item in (profile.get("keywords") or [])),
                json.dumps(profile.get("projects") or [], ensure_ascii=False),
            ]
        ).lower()
        evidence_terms = {
            token for token in re.findall(r"[a-z0-9+#]+", evidence) if len(token) > 3
        }
        domain_points = 10 if any(term in job_text for term in evidence_terms) else 0
        score = round(required_points + preferred_points + role_points + domain_points)
        reasons = []
        if strong:
            reasons.append(f"strong: {', '.join(strong)}")
        if partial:
            reasons.append(f"partial: {', '.join(partial)}")
        missing = missing_required + missing_preferred
        if missing:
            reasons.append(f"missing: {', '.join(missing)}")
        results.append(
            {
                "path": str(path),
                "score": score,
                "strong_matches": list(dict.fromkeys(strong)),
                "partial_matches": list(dict.fromkeys(partial)),
                "missing_required": missing_required,
                "missing_preferred": missing_preferred,
                "reasoning": "; ".join(reasons) or "No documented skill match.",
            }
        )
    return sorted(results, key=lambda item: (-item["score"], item["path"].lower()))


def location_points(job: dict, config: dict) -> tuple[int, list[str]]:
    location = (job.get("location") or "").lower()
    workplace = (job.get("workplace_type") or "").lower()
    if not location and not workplace:
        return 0, ["location/workplace unavailable"]
    if "karachi" in location or "karāchi" in location:
        return 10, []
    if "pakistan" in location and "remote" in workplace:
        return 9, []
    if "pakistan" in location:
        return 7, []
    if config.get("remote", True) and "remote" in workplace:
        return 5, []
    return 0, ["location is not compatible"]


def _profile_skills(profile: dict) -> set[str]:
    return {
        canonical_skill(skill)
        for skill in list(profile.get("skills") or [])
        + list((profile.get("skill_years") or {}).keys())
    }


def _component(earned, maximum, evidence=None):
    return {
        "earned": int(round(max(0, min(maximum, earned)))),
        "maximum": maximum,
        "evidence": evidence or [],
    }


def score_job(job: dict, resume_profile: dict, role_profiles: list[dict], config: dict) -> dict:
    weights = config.get("fit_score_weights", DEFAULT_FIT_SCORE_WEIGHTS)
    available = _profile_skills(resume_profile)
    required = job.get("required_skills") or []
    preferred = job.get("preferred_skills") or []
    strong, partial, missing_required, missing_preferred = [], [], [], []
    required_credit = 0.0
    for skill in required:
        canonical = canonical_skill(skill)
        if canonical in available:
            strong.append(skill)
            required_credit += 1
        elif _related_skill(canonical, available):
            partial.append(skill)
            required_credit += 0.5
        else:
            missing_required.append(skill)
    for skill in preferred:
        canonical = canonical_skill(skill)
        if canonical in available:
            strong.append(skill)
        elif _related_skill(canonical, available):
            partial.append(skill)
        else:
            missing_preferred.append(skill)

    role_texts = [job.get("title"), *(job.get("matched_roles") or [])]
    configured_role_texts = []
    for profile in role_profiles:
        configured_role_texts.extend([profile.get("name"), *(profile.get("keywords") or [])])
    role_overlap = _role_tokens(role_texts).intersection(_role_tokens(configured_role_texts))

    job_text = " ".join(
        [
            job.get("title") or "",
            job.get("description") or "",
            *(job.get("responsibilities") or []),
            *required,
            *preferred,
        ]
    ).lower()
    backend_terms = {"backend", "api", "django", "fastapi", "laravel", "server"}
    profile_text = " ".join(
        [
            *(resume_profile.get("roles") or []),
            *(resume_profile.get("target_role_categories") or []),
            *available,
        ]
    ).lower()
    backend_job = any(term in job_text for term in backend_terms)
    backend_profile = any(term in profile_text for term in backend_terms)

    required_years = job.get("years_experience")
    candidate_years = resume_profile.get("total_years_experience")
    if required_years is None:
        experience_ratio = 0.5
        experience_evidence = ["job experience requirement unavailable"]
    elif isinstance(candidate_years, (int, float)) and candidate_years >= required_years:
        experience_ratio = 1
        experience_evidence = [f"{candidate_years} years meets {required_years}"]
    elif isinstance(candidate_years, (int, float)) and candidate_years >= required_years - 1:
        experience_ratio = 0.7
        experience_evidence = [f"{candidate_years} years is within one year of {required_years}"]
    else:
        experience_ratio = 0
        experience_evidence = [f"experience below {required_years} years"]

    location_base, location_concerns = location_points(job, config)
    ai_job = bool(re.search(r"\b(ai|llm)\b|generative|machine learning", job_text))
    ai_profile = any(skill in available for skill in {"langchain", "langgraph", "rag"}) or "ai" in profile_text
    database_terms = {"postgresql", "mysql", "mongodb", "sql", "database", "airflow", "spark"}
    database_job = any(term in job_text for term in database_terms)
    database_profile = bool(available.intersection(database_terms))
    cloud_terms = {"aws", "azure", "gcp", "docker", "kubernetes", "cloud"}
    cloud_job = any(term in job_text for term in cloud_terms)
    cloud_profile = bool(available.intersection(cloud_terms))

    components = {
        "role_title": _component(weights["role_title"] if role_overlap else 0, weights["role_title"], sorted(role_overlap)),
        "required_skills": _component(
            weights["required_skills"] * required_credit / len(required) if required else 0,
            weights["required_skills"],
            strong + partial,
        ),
        "backend_api": _component(
            weights["backend_api"] if backend_job and backend_profile else 0,
            weights["backend_api"],
            ["backend/API job and resume evidence"] if backend_job and backend_profile else [],
        ),
        "experience_seniority": _component(
            weights["experience_seniority"] * experience_ratio,
            weights["experience_seniority"],
            experience_evidence,
        ),
        "location_workplace": _component(
            weights["location_workplace"] * location_base / 10,
            weights["location_workplace"],
            [job.get("location") or "unknown"],
        ),
        "ai": _component(weights["ai"] if ai_job and ai_profile else 0, weights["ai"], ["AI relevance"] if ai_job and ai_profile else []),
        "database_data": _component(
            weights["database_data"] if database_job and database_profile else 0,
            weights["database_data"],
            ["database/data relevance"] if database_job and database_profile else [],
        ),
        "cloud_infrastructure": _component(
            weights["cloud_infrastructure"] if cloud_job and cloud_profile else 0,
            weights["cloud_infrastructure"],
            ["cloud/infrastructure relevance"] if cloud_job and cloud_profile else [],
        ),
    }
    score = max(0, min(100, sum(component["earned"] for component in components.values())))
    concerns = list(job.get("analysis_concerns") or []) + location_concerns
    if not required:
        concerns.append("requirements unavailable")
    if "senior" in str(job.get("seniority") or "").lower() and (
        not isinstance(candidate_years, (int, float)) or candidate_years < 5
    ):
        concerns.append("seniority may exceed documented experience")

    matched_names = set(job.get("matched_roles") or [])
    thresholds = [
        profile.get("minimum_fit_score", config.get("minimum_fit_score", 70))
        for profile in role_profiles
        if not matched_names or profile.get("name") in matched_names
    ]
    minimum = min(thresholds) if thresholds else config.get("minimum_fit_score", 70)
    stretch = config.get("stretch_fit_score", 60)
    tier = "A" if score >= 80 else "B" if score >= minimum else "C" if score >= stretch else "D"
    summary = [f"{name} {value['earned']}/{value['maximum']}" for name, value in components.items() if value["earned"]]
    return {
        "score": score,
        "tier": tier,
        "components": components,
        "strong_matches": list(dict.fromkeys(strong)),
        "partial_matches": list(dict.fromkeys(partial)),
        "missing_required": missing_required,
        "missing_preferred": missing_preferred,
        "concerns": list(dict.fromkeys(concerns)),
        "reasoning": f"{score}/100: " + (", ".join(summary) if summary else "no scored alignment"),
    }


def rank_jobs(scored_jobs: list[dict], config: dict) -> list[dict]:
    excluded = {_identity_text(company) for company in config.get("excluded_companies", [])}
    preferred = {_identity_text(company) for company in config.get("preferred_companies", [])}

    def ordinal(value):
        try:
            return date.fromisoformat(value).toordinal()
        except (TypeError, ValueError):
            return 0

    filtered = [
        item
        for item in scored_jobs
        if _identity_text(item.get("job", {}).get("company")) not in excluded
    ]
    return sorted(
        filtered,
        key=lambda item: (
            0 if item.get("fit", {}).get("tier") in ("A", "B") else 1,
            -item.get("fit", {}).get("score", 0),
            item.get("role_priority", 99),
            -ordinal(item.get("job", {}).get("posting_date")),
            item.get("job", {}).get("applicant_count")
            if item.get("job", {}).get("applicant_count") is not None
            else float("inf"),
            -int(_identity_text(item.get("job", {}).get("company")) in preferred),
        ),
    )
