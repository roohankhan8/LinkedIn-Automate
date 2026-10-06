# Job Intelligence Foundation Design

## Purpose

Build the first safe increment of the job-search assistant: collect jobs across configurable role profiles, normalize and deduplicate them, understand their requirements, rank the existing resumes, and produce a transparent fit score before any application is opened.

This increment optimizes for application quality while preserving the existing LinkedIn search, resume discovery, Easy Apply implementation, JSON tracking files, dashboard endpoints, and command-line compatibility.

## Scope

### Included

- Backward-compatible configuration normalization.
- Configurable target-role profiles with priorities and thresholds.
- Seven-day search-window support, with 1-, 3-, and 7-day values.
- A normalized job representation.
- Cross-role duplicate detection and `matched_roles` merging.
- Deterministic parsing for URLs, applicant counts, dates, locations, workplace types, seniority, experience, education, and common skills.
- One validated Gemini call per job only when semantic requirement extraction is needed.
- Transparent 0–100 job-fit scoring.
- One content-addressed structured profile per resume.
- Profile-based ranking of every available resume through the existing `choose_resume_path()` entry point.
- Unit tests that do not call LinkedIn or Gemini.

### Excluded

- Easy Apply behavior changes, preparation mode, and human confirmation.
- QA-cache schema changes and high-risk-question handling.
- SQLite/application lifecycle tracking.
- External-site application workflows.
- Dashboard recommendations, queues, and analytics.
- Resume-file modification.
- Documentation changes outside this design and its later implementation plan.

These are separate increments because each changes a different trust boundary and can be shipped and verified independently.

## Existing System Constraints

- `/jobs/search/` and its current card-hydration behavior remain unchanged.
- `choose_resume_path(ctx, config)` remains importable and returns a path or `None`.
- Existing `role`, `roles`, `past_24_hours`, `resume_path`, and `default_resume_path` settings remain accepted.
- Existing `resume_profile.json`, `applied_jobs.json`, `not_targeted_jobs.json`, `not_targeted_jobs.txt`, and `qa_cache.json` remain readable and are not migrated in this increment.
- Existing Easy Apply field collection and filling are not modified.
- No new runtime dependency is introduced; the implementation uses Python's standard library and installed packages.

## Configuration

`load_config()` will read JSON as it does now and pass it through `normalize_config(raw)`. The normalized result supplies defaults without rewriting the user's file.

The preferred schema is:

```json
{
  "target_roles": [
    {
      "name": "Backend Engineer",
      "priority": 1,
      "keywords": ["backend engineer", "python", "django", "fastapi", "api"],
      "minimum_fit_score": 70,
      "enabled": true
    }
  ],
  "locations": ["Karachi", "Pakistan"],
  "remote": true,
  "posted_within_days": 7,
  "minimum_fit_score": 70,
  "stretch_fit_score": 60,
  "fit_score_weights": {
    "role_title": 20,
    "required_skills": 30,
    "backend_api": 15,
    "experience_seniority": 10,
    "location_workplace": 10,
    "ai": 5,
    "database_data": 5,
    "cloud_infrastructure": 5
  },
  "preferred_companies": [],
  "excluded_companies": []
}
```

Compatibility rules:

1. A non-empty `target_roles` list is authoritative.
2. Otherwise, each string in `roles` becomes an enabled profile with priority `1`, empty extra keywords, and the global minimum score.
3. Otherwise, `role` becomes the single enabled profile.
4. `past_24_hours: true` maps to one day only when `posted_within_days` is absent; otherwise the new setting wins.
5. `posted_within_days` accepts only `1`, `3`, or `7` and defaults to `7`.
6. Fit weights must be non-negative numbers totaling exactly `100`; invalid configuration raises `ValueError` before opening a browser.
7. Each enabled role requires a non-empty name, priority `1`, `2`, or `3`, a list of strings for keywords, and a score from `0` through `100`.
8. Old configuration keys remain in the normalized dictionary so existing consumers continue to work.

The complete role list supplied in the request will be added to `config.example.json`, not hard-coded into the search loop.

- Priority 1: Backend Engineer; Backend Developer; Python Backend Engineer; Python Software Engineer; Software Engineer Python; Django Backend Engineer; Django Developer; FastAPI Developer; Backend Engineer APIs; Full Stack Engineer Python React.
- Priority 2: AI Backend Engineer; AI Software Engineer; AI Application Engineer; LLM Engineer; Generative AI Engineer; AI Integration Engineer; Software Engineer AI; Full Stack Engineer AI; Data Engineer.
- Priority 3: Laravel Developer; PHP Backend Developer; PHP Laravel Software Engineer; Full Stack Developer; Node.js Backend Engineer; Software Engineer.

Each example profile uses its role name as the initial keyword and may add only direct technology or role synonyms. Users can edit these lists without changing Python code.

## Normalized Job

`normalize_job(raw, matched_role)` returns a plain dictionary so it remains JSON-compatible with the existing system:

```json
{
  "job_id": "4471547091",
  "url": "https://www.linkedin.com/jobs/view/4471547091",
  "title": "Backend Engineer",
  "company": "Example",
  "location": "Karachi, Sindh, Pakistan",
  "employment_type": "Full-time",
  "workplace_type": "On-site",
  "seniority": "Mid-Senior level",
  "posting_date": "2026-10-02",
  "applicant_count": 42,
  "description": "...",
  "required_skills": ["Python", "Django"],
  "preferred_skills": ["Docker"],
  "years_experience": 3,
  "education_requirement": "Bachelor's degree",
  "responsibilities": ["Build REST APIs"],
  "application_method": "EASY_APPLY",
  "matched_roles": ["Backend Engineer"]
}
```

Missing values are `null`, empty strings, or empty lists according to the field type; they are never invented. Dates use ISO `YYYY-MM-DD` when a relative posting age can be resolved against the current local date.

The canonical URL strips tracking parameters. When a LinkedIn job ID exists, it becomes `https://www.linkedin.com/jobs/view/<id>`.

## Duplicate Detection

`job_key(job)` uses the first available identity:

1. `linkedin:<job_id>`
2. `url:<canonical_url>`
3. `details:<normalized company>|<normalized title>|<normalized location>`

Normalization lowercases, trims, collapses whitespace, and removes punctuation without changing word order.

`merge_jobs(existing, incoming)` keeps the more complete non-empty value for every field, unions skills, responsibilities, and `matched_roles` without duplicates, and keeps the lowest known applicant count only when the two observations are from the same job. Search discovery holds this map across every enabled role so one job is scored once.

## Job Understanding

Deterministic parsing runs first:

- Job ID and canonical URL from URL components.
- Applicant count from the structured header text.
- Workplace and employment types from detail-panel labels, never the description alone.
- Posting age/date from structured header text.
- Seniority, years of experience, education phrases, and known technical skills from bounded regular expressions and canonical skill aliases.
- Application method from the presence of Easy Apply versus an external apply action.

Gemini is called only when required/preferred skills, responsibilities, experience, education, or ambiguous classification remain materially incomplete after deterministic parsing. The prompt includes the job text and demands exactly the normalized semantic fields, not identity or tracking fields already known deterministically.

`validate_job_analysis(value)` rejects non-object responses, wrong field types, unexpected scalar/list shapes, scores, or invented identity fields. A malformed response logs a warning and leaves semantic fields at their deterministic values; it does not abort discovery.

## Resume Profile Cache

Profiles are stored under `resume_profiles/<sha256>.json`, where the identifier is the SHA-256 digest of the resume bytes. Unchanged content therefore never triggers another extraction, while changing one resume creates only one new profile.

Each profile contains the existing factual fields plus:

- `roles`
- `experience`
- `domains`
- `projects`
- `education`
- `keywords`
- `target_role_categories`
- `_schema_version`
- `_resume_path`
- `_resume_sha256`
- `_resume_text`

`validate_resume_profile(value)` verifies required collection and scalar types. Facts absent from the resume remain `null` or empty collections. Gemini receives the existing factual-only system instruction, strengthened to prohibit inferred employers, qualifications, authorization, compensation, or unsupported experience.

Compatibility behavior:

- `get_or_build_profile(resume_path, force=False, gemini=None)` retains its signature.
- A matching legacy `resume_profile.json` can be returned and copied into the content-addressed cache after validation.
- Building a missing profile requires the passed Gemini client, as it does in the production search flow.
- `force=True` rebuilds only the selected resume's cache entry.
- The legacy global file is not overwritten when processing other resumes.

## Resume Ranking

`rank_resumes(job, resume_profiles)` returns every candidate sorted by a transparent 0–100 match:

- Required skill coverage: 60 points.
- Preferred skill coverage: 15 points.
- Role/category alignment: 15 points.
- Domain/project evidence: 10 points.

Skill aliases such as `REST API`/`REST APIs`, `Postgres`/`PostgreSQL`, and `Node`/`Node.js` are canonicalized deterministically. Exact canonical matches are strong; related aliases are partial and receive half credit. Missing required skills are reported explicitly.

Each result contains:

```json
{
  "path": "resumes/backend.pdf",
  "score": 94,
  "strong_matches": ["Python", "Django", "REST APIs"],
  "partial_matches": ["AI integration"],
  "missing_required": [],
  "missing_preferred": ["Kubernetes"],
  "reasoning": "Matched all required backend skills; partial AI evidence; Kubernetes absent."
}
```

`choose_resume_path()` discovers the existing resume files, loads or builds their profiles, calls `rank_resumes()`, and returns the top path. Its production caller will pass the existing Gemini client. If no validated profile can be loaded or built, it falls back to an existing configured resume path; filename scoring remains only as an offline compatibility fallback and emits a warning.

## Job Fit Scoring

The selected resume profile is scored against the normalized job. Each configurable component returns earned points, maximum points, and evidence:

- Role/title match: `20`
- Required technical skills: `30`
- Backend/API relevance: `15`
- Experience/seniority compatibility: `10`
- Location/workplace compatibility: `10`
- AI relevance: `5`
- Database/data-engineering relevance: `5`
- Cloud/infrastructure relevance: `5`

Rules are deterministic:

- Title matching uses normalized role names, configured keywords, and canonical role categories.
- Skill points are proportional to strong and half-credit partial matches. If requirements could not be extracted, this component earns zero and adds an extraction concern rather than assuming a match.
- Backend, AI, database, and cloud components require both job relevance and resume evidence.
- Experience earns full points when the profile meets a stated requirement, seven points when within one year, zero when further below, and five when the job does not state a requirement. Seniority conflicts add a concern.
- Location earns 10 for Karachi, 9 for explicitly remote within Pakistan, 7 for Pakistan, 5 for an explicitly remote-compatible foreign location, and 0 otherwise. Description text alone never establishes remote status.
- Excluded companies are filtered before scoring. Preferred companies are a tie-breaker only and never add fit points.
- Role priority, posting recency, applicant count, and preferred-company status sort otherwise equal scores but do not obscure the technical score.

The output is:

```json
{
  "score": 87,
  "tier": "A",
  "components": {
    "role_title": {"earned": 18, "maximum": 20, "evidence": ["Backend Engineer"]}
  },
  "strong_matches": [],
  "partial_matches": [],
  "missing_required": [],
  "missing_preferred": [],
  "concerns": [],
  "reasoning": "87/100: strong role, Python API, location, and experience alignment; partial cloud evidence."
}
```

Tier thresholds are `A >= 80`, `B >= minimum_fit_score`, `C >= stretch_fit_score`, and `D` otherwise. A role profile's `minimum_fit_score` overrides the global recommendation threshold for jobs matched through that profile; when several profiles match, the lowest applicable threshold is used and all profile names remain visible.

## Discovery Data Flow

1. Load and validate configuration.
2. Discover all resumes and load/build their profiles once.
3. Search every enabled role profile using its configured keywords and requested time window.
4. Normalize each discovered job and add its matching profile name.
5. Deduplicate and merge jobs across searches.
6. Deterministically parse each job; request validated semantic extraction only when needed.
7. Rank resumes and select the best one through `choose_resume_path()`.
8. Score the job using the selected profile.
9. Sort the complete set by recommendation eligibility, fit score, role priority, recency, applicant count, and preferred-company tie-break.
10. Print the ranked recommendations and stop at the existing application boundary.

Application execution remains unchanged in this increment; preparation-by-default and explicit submission opt-in belong to the next increment. Discovery and scoring must preserve the current application call boundary and must not add any new automatic-submission path.

## Error Handling

- Invalid configuration fails before browser startup with a field-specific message.
- One malformed job or AI response is skipped or degraded without losing other discoveries.
- Resume extraction failure identifies the file and continues ranking remaining resumes.
- No matching resume prevents recommendation and records a concern; it does not silently select an unrelated file.
- Cache writes use a temporary file followed by `os.replace()` so interruption cannot leave partial JSON.
- Unknown location/workplace data lowers the location component and remains visible as a concern.

## Files

### Create

- `job_intelligence.py`: configuration normalization, job normalization and merging, deterministic analysis, AI-response validation, fit scoring, and resume ranking.
- `tests/test_job_intelligence.py`: normalization, deduplication, scoring, location, role, and configuration tests.
- `tests/test_resume_profiles.py`: content-addressed caching, stale-file behavior, and multi-resume ranking tests.
- `tests/test_gemini_client.py`: malformed structured-response tests.

### Modify

- `linkedin_search.py`: collect jobs across profiles, call the intelligence functions, retain `choose_resume_path()`, and use the ranked result.
- `resume_profile.py`: expand and validate the factual profile schema and use content-addressed caches.
- `gemini_client.py`: expose strict structured-result validation without changing providers.
- `config.example.json`: provide the requested target-role profiles and new defaults.
- `.gitignore`: ignore generated `resume_profiles/*.json` cache files while retaining the directory if needed.
- `test_resume_selection.py`: move its assertion into the new test suite or adapt it to profile-based ranking.

### Unchanged

- `easy_apply.py`
- `qa_store.py`
- `app.py`
- `templates/`
- `static/`
- Existing tracking JSON/TXT files

## Verification

Tests will cover:

- Old `role` and `roles` configuration migration.
- Invalid roles, windows, thresholds, and weight totals.
- Job ID, canonical URL, and detail-key deduplication in reliability order.
- Merging `matched_roles` and retaining more complete job data.
- Karachi, remote Pakistan, Pakistan, remote foreign, unknown, and incompatible locations.
- Transparent component totals and exact tier boundaries.
- Preferred-company tie-breaking without score inflation.
- Resume cache reuse for unchanged bytes and rebuild after a content change.
- Legacy profile-cache compatibility.
- Ranking all resumes and retaining `choose_resume_path()` behavior.
- Missing requirements and malformed Gemini responses.

Required commands after implementation:

```powershell
.\venv\Scripts\python.exe -m unittest discover -s tests -v
.\venv\Scripts\python.exe -m py_compile linkedin_search.py easy_apply.py qa_store.py resume_profile.py gemini_client.py linkedin_login.py job_intelligence.py
```

No test will open LinkedIn, submit an application, or call Gemini.
