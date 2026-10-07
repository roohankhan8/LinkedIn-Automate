"""Extract factual resume profiles and cache each one by content hash."""

import hashlib
import json
import os
import tempfile
from pathlib import Path

from pypdf import PdfReader

from gemini_client import Gemini

PROFILE_PATH = "resume_profile.json"
PROFILE_CACHE_DIR = Path(__file__).resolve().parent / "resume_profiles"
PROFILE_SCHEMA_VERSION = 2

EXTRACTION_SYSTEM = (
    "You extract structured facts from resumes for filling out job application forms. "
    "Only use information present in the resume. Use null for anything missing. "
    "Never invent employers, degrees, or contact details."
)

EXTRACTION_PROMPT = """Extract the following fields from this resume and return JSON with exactly these keys:

{{
  "full_name": string|null,
  "first_name": string|null,
  "last_name": string|null,
  "email": string|null,
  "phone": string|null,
  "phone_country_code": string|null,
  "city": string|null,
  "state": string|null,
  "country": string|null,
  "linkedin_url": string|null,
  "github_url": string|null,
  "portfolio_url": string|null,
  "headline": string|null,
  "summary": string|null,
  "total_years_experience": number|null,
  "current_title": string|null,
  "current_company": string|null,
  "highest_degree": string|null,
  "field_of_study": string|null,
  "university": string|null,
  "graduation_year": number|null,
  "skills": [string],
  "skill_years": {{"skill name": number}},
  "roles": [string],
  "experience": [object],
  "domains": [string],
  "projects": [object],
  "education": [object],
  "certifications": [string],
  "keywords": [string],
  "target_role_categories": [string],
  "languages": [string],
  "work_authorization": string|null,
  "requires_visa_sponsorship": boolean|null,
  "willing_to_relocate": boolean|null,
  "notice_period_days": number|null,
  "current_ctc": string|null,
  "expected_ctc": string|null
}}

"skill_years" should map each significant skill to your best estimate of years of
hands-on experience, inferred from the dated roles and projects in the resume.

Resume text:
---
{resume_text}
---
"""


def extract_pdf_text(path):
    reader = PdfReader(path)
    parts = [page.extract_text() or "" for page in reader.pages]
    text = "\n".join(parts).strip()
    if not text:
        raise ValueError(f"No text extracted from {path}. Is it a scanned image PDF?")
    return text


def read_resume_text(path):
    ext = os.path.splitext(path)[1].lower()
    if ext == ".pdf":
        return extract_pdf_text(path)
    if ext in (".txt", ".md"):
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read()
    raise ValueError(f"Unsupported resume format '{ext}'. Use PDF or TXT.")


def build_profile(resume_path, gemini=None):
    print(f"Reading resume: {resume_path}")
    resume_text = read_resume_text(resume_path)
    print(f"Extracted {len(resume_text)} characters. Asking Gemini for structured insights...")

    gemini = gemini or Gemini()
    profile = gemini.generate_json_object(
        EXTRACTION_PROMPT.format(resume_text=resume_text[:60000]),
        system=EXTRACTION_SYSTEM,
    )
    profile = validate_resume_profile(profile)
    profile["_schema_version"] = PROFILE_SCHEMA_VERSION
    profile["_resume_path"] = str(Path(resume_path).resolve())
    profile["_resume_sha256"] = resume_digest(resume_path)
    profile["_resume_text"] = resume_text[:20000]
    return profile


def save_profile(profile, path=PROFILE_PATH):
    _atomic_save_profile(profile, Path(path))
    print(f"Profile saved to {path}")


def load_profile(path=PROFILE_PATH):
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def resume_digest(path):
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def profile_cache_path(resume_path, cache_dir=PROFILE_CACHE_DIR):
    return Path(cache_dir) / f"{resume_digest(resume_path)}.json"


PROFILE_LIST_FIELDS = {
    "skills",
    "roles",
    "experience",
    "domains",
    "projects",
    "education",
    "certifications",
    "languages",
    "keywords",
    "target_role_categories",
}


def validate_resume_profile(value):
    if not isinstance(value, dict):
        raise ValueError("resume profile must be a JSON object")
    profile = dict(value)
    for key in PROFILE_LIST_FIELDS:
        item = profile.get(key, [])
        if not isinstance(item, list):
            raise ValueError(f"resume profile field {key} must be a list")
        profile[key] = item
    skill_years = profile.get("skill_years", {})
    if not isinstance(skill_years, dict):
        raise ValueError("resume profile field skill_years must be an object")
    if any(
        not isinstance(key, str)
        or isinstance(years, bool)
        or not isinstance(years, (int, float))
        or years < 0
        for key, years in skill_years.items()
    ):
        raise ValueError("resume profile skill_years must map skills to non-negative numbers")
    profile["skill_years"] = skill_years
    return profile


def _atomic_save_profile(profile, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, suffix=".tmp", delete=False
    )
    try:
        with handle:
            json.dump(profile, handle, indent=2, ensure_ascii=False)
        os.replace(handle.name, path)
    except Exception:
        try:
            os.unlink(handle.name)
        except OSError:
            pass
        raise


def _decorate_cached_profile(profile, resume_path, digest):
    profile = validate_resume_profile(profile)
    profile["_schema_version"] = PROFILE_SCHEMA_VERSION
    profile["_resume_path"] = str(Path(resume_path).resolve())
    profile["_resume_sha256"] = digest
    return profile


def get_or_build_profile(resume_path, force=False, gemini=None, cache_dir=PROFILE_CACHE_DIR):
    """Return this resume's cached profile, rebuilding only when its bytes change."""
    if not os.path.exists(resume_path):
        raise FileNotFoundError(f"Resume not found: {resume_path}")

    digest = resume_digest(resume_path)
    cache_path = Path(cache_dir) / f"{digest}.json"
    if cache_path.exists() and not force:
        profile = validate_resume_profile(json.loads(cache_path.read_text(encoding="utf-8")))
        if profile.get("_resume_sha256") == digest:
            print(f"Using cached resume profile from {cache_path}")
            return profile

    if not force:
        legacy = load_profile(PROFILE_PATH)
        cached_path = legacy.get("_resume_path") if isinstance(legacy, dict) else None
        if cached_path and os.path.normcase(os.path.abspath(resume_path)) == os.path.normcase(cached_path):
            profile = _decorate_cached_profile(legacy, resume_path, digest)
            _atomic_save_profile(profile, cache_path)
            print(f"Imported legacy resume profile into {cache_path}")
            return profile

    profile = build_profile(resume_path, gemini=gemini)
    _atomic_save_profile(profile, cache_path)
    print(f"Profile saved to {cache_path}")
    return profile


def profile_for_prompt(profile):
    """Strip internal keys before sending the profile to the model."""
    return {k: v for k, v in profile.items() if not k.startswith("_")}


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Extract resume insights into resume_profile.json")
    parser.add_argument("resume", nargs="?", help="Path to resume PDF (defaults to config.json resume_path)")
    parser.add_argument("--force", action="store_true", help="Rebuild even if a cached profile exists")
    args = parser.parse_args()

    resume = args.resume
    if not resume:
        with open("config.json", "r", encoding="utf-8") as f:
            resume = json.load(f).get("resume_path")
    if not resume:
        raise SystemExit("Provide a resume path or set 'resume_path' in config.json")

    result = get_or_build_profile(resume, force=args.force)
    print(json.dumps(profile_for_prompt(result), indent=2, ensure_ascii=False))
