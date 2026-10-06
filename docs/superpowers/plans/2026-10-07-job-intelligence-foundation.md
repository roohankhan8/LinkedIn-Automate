# Job Intelligence Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add cross-role job normalization, deduplication, transparent fit scoring, and profile-based multi-resume selection before the existing application boundary.

**Architecture:** Put deterministic, browser-independent intelligence in one new `job_intelligence.py` module. Keep LinkedIn DOM operations in `linkedin_search.py`, keep resume extraction in `resume_profile.py`, and preserve `choose_resume_path()` as the sole resume-selection entry point. Collect and rank all discoveries before invoking the existing Easy Apply path.

**Tech Stack:** Python 3 standard library (`dataclasses` are unnecessary; use JSON-compatible dictionaries), `unittest`, existing Playwright, pypdf, and Gemini wrapper.

**Spec:** `docs/superpowers/specs/2026-10-07-job-intelligence-foundation-design.md`

## Global Constraints

- Do not modify `easy_apply.py`, `qa_store.py`, `app.py`, `templates/`, `static/`, resume files, or existing tracking data.
- Preserve `/jobs/search/`, card hydration, Easy Apply field handling, JSON tracking compatibility, and `choose_resume_path(ctx, config)` callers.
- Do not add dependencies or call LinkedIn/Gemini in tests.
- Use deterministic parsing before Gemini and validate every AI-produced object.
- Keep existing application execution behavior unchanged in this increment; do not add an automatic-submission path.
- Generated resume profiles live under `resume_profiles/` and are ignored by Git.

## Review Focus

- A valid old config containing only `role` must produce one enabled target profile; covered in Task 1.
- Two observations with different URLs but the same LinkedIn ID must merge their roles and richer fields; covered in Task 2.
- Unknown requirements must lower confidence instead of earning assumed skill points; covered in Task 6.
- Changing one resume must invalidate only that resume's cache; covered in Task 4.
- A disappeared modal must not be reinterpreted by this increment as successful submission; Task 7 verifies the existing `apply_to_current_job()` result remains the only tracking signal.

---

### Task 1: Backward-Compatible Configuration

**Files:**
- Create: `job_intelligence.py`
- Create: `tests/__init__.py`
- Create: `tests/test_job_intelligence.py`
- Modify: `linkedin_search.py:85-90,157-169,583-629`
- Modify: `config.example.json`

**Interfaces:**
- Produces: `normalize_config(raw: dict) -> dict`
- Produces: `enabled_role_profiles(config: dict) -> list[dict]`
- Produces: `recency_filter(days: int) -> str`
- Preserves: `load_config(path: str = "config.json") -> dict`

- [ ] **Step 1: Write failing configuration tests**

Add `JobIntelligenceConfigTests` with assertions equivalent to:

```python
def test_legacy_role_becomes_target_profile(self):
    config = normalize_config({"role": "Backend Developer"})
    self.assertEqual([p["name"] for p in enabled_role_profiles(config)], ["Backend Developer"])
    self.assertEqual(config["posted_within_days"], 7)

def test_legacy_roles_and_past_24_hours_are_supported(self):
    config = normalize_config({"roles": ["Backend", "Data Engineer"], "past_24_hours": True})
    self.assertEqual(len(enabled_role_profiles(config)), 2)
    self.assertEqual(config["posted_within_days"], 1)

def test_invalid_weight_total_is_rejected(self):
    with self.assertRaisesRegex(ValueError, "total 100"):
        normalize_config({"role": "Backend", "fit_score_weights": {"role_title": 10}})

def test_search_windows_map_to_linkedin_filters(self):
    self.assertEqual(recency_filter(1), "r86400")
    self.assertEqual(recency_filter(3), "r259200")
    self.assertEqual(recency_filter(7), "r604800")
```

Also test invalid `posted_within_days`, priority, keywords type, empty name, and score range.

- [ ] **Step 2: Run the focused tests and confirm failure**

Run: `.\venv\Scripts\python.exe -m unittest tests.test_job_intelligence.JobIntelligenceConfigTests -v`

Expected: import or attribute failures because `job_intelligence.py` does not exist.

- [ ] **Step 3: Implement minimal normalization and validation**

Define the exact defaults from the spec, preserve unknown legacy keys, and ensure explicit `posted_within_days` wins over `past_24_hours`. Validate only enabled profiles returned by `enabled_role_profiles()`.

Update `load_config()` to return `normalize_config(json.load(...))`. Update `search_url()` to use `recency_filter(config["posted_within_days"])` rather than `past_24_hours`.

Populate `config.example.json` with all 25 requested role profiles at priorities 1–3 and the approved scoring/configuration defaults.

- [ ] **Step 4: Run tests and compilation**

Run: `.\venv\Scripts\python.exe -m unittest tests.test_job_intelligence.JobIntelligenceConfigTests -v`

Expected: all configuration tests pass.

Run: `.\venv\Scripts\python.exe -m py_compile job_intelligence.py linkedin_search.py`

Expected: exit code `0`.

- [ ] **Step 5: Commit**

```powershell
git add job_intelligence.py tests/__init__.py tests/test_job_intelligence.py linkedin_search.py config.example.json
git commit -m "feat: normalize job search configuration"
```

### Task 2: Normalized Jobs and Cross-Role Deduplication

**Files:**
- Modify: `job_intelligence.py`
- Modify: `tests/test_job_intelligence.py`

**Interfaces:**
- Consumes: normalized role profile names from Task 1.
- Produces: `canonical_job_url(url: str, job_id: str | None = None) -> str`
- Produces: `normalize_job(raw: dict, matched_role: str, today: date | None = None) -> dict`
- Produces: `job_key(job: dict) -> str`
- Produces: `merge_jobs(existing: dict, incoming: dict) -> dict`
- Produces: `deduplicate_jobs(jobs: list[dict]) -> list[dict]`

- [ ] **Step 1: Write failing normalization and deduplication tests**

Cover:

```python
def test_linkedin_id_is_preferred_and_url_is_canonical(self):
    job = normalize_job({"url": "https://www.linkedin.com/jobs/search/?currentJobId=123&trk=x"}, "Backend")
    self.assertEqual(job["job_id"], "123")
    self.assertEqual(job["url"], "https://www.linkedin.com/jobs/view/123")
    self.assertEqual(job_key(job), "linkedin:123")

def test_duplicate_observations_merge_roles_and_richer_fields(self):
    jobs = deduplicate_jobs([
        normalize_job({"url": "https://www.linkedin.com/jobs/view/123", "title": "Backend Engineer"}, "Backend Engineer"),
        normalize_job({"url": "https://www.linkedin.com/jobs/search/?currentJobId=123", "description": "Build APIs"}, "AI Software Engineer"),
    ])
    self.assertEqual(len(jobs), 1)
    self.assertEqual(jobs[0]["matched_roles"], ["Backend Engineer", "AI Software Engineer"])
    self.assertEqual(jobs[0]["description"], "Build APIs")
```

Also test canonical-URL fallback, normalized details fallback, ISO dates from `5 days ago`, list unions, and same-job applicant-count merging.

- [ ] **Step 2: Run focused tests and confirm failure**

Run: `.\venv\Scripts\python.exe -m unittest tests.test_job_intelligence.JobNormalizationTests -v`

Expected: missing-function failures.

- [ ] **Step 3: Implement the normalized dictionary and merge rules**

Use `urllib.parse`, `re`, and `datetime.date`. Produce every field named in the spec with stable empty values. Use first-available identity ordering and preserve deterministic list order with `dict.fromkeys()`.

- [ ] **Step 4: Run focused tests**

Run: `.\venv\Scripts\python.exe -m unittest tests.test_job_intelligence.JobNormalizationTests -v`

Expected: all normalization tests pass.

- [ ] **Step 5: Commit**

```powershell
git add job_intelligence.py tests/test_job_intelligence.py
git commit -m "feat: normalize and deduplicate discovered jobs"
```

### Task 3: Deterministic and Validated Job Analysis

**Files:**
- Modify: `job_intelligence.py`
- Modify: `gemini_client.py`
- Modify: `tests/test_job_intelligence.py`
- Create: `tests/test_gemini_client.py`

**Interfaces:**
- Consumes: normalized job dictionaries from Task 2.
- Produces: `deterministic_job_analysis(job: dict) -> dict`
- Produces: `validate_job_analysis(value: object) -> dict`
- Produces: `analyze_job(job: dict, gemini=None) -> dict`
- Produces: `Gemini.generate_json_object(prompt, system=None, temperature=0.1) -> dict`

- [ ] **Step 1: Write failing deterministic-analysis tests**

Assert extraction of required/preferred skills, years, education, responsibilities, seniority, employment type, and structured workplace type from representative job text/header fields. Include:

```python
def test_description_remote_word_does_not_set_workplace_type(self):
    job = normalize_job({"location": "Lahore, Pakistan", "description": "Collaborate with remote teams"}, "Backend")
    self.assertIsNone(deterministic_job_analysis(job)["workplace_type"])

def test_malformed_ai_analysis_keeps_deterministic_data(self):
    gemini = Mock()
    gemini.generate_json_object.return_value = {"required_skills": "Python"}
    result = analyze_job(normalize_job({"description": "Requires Python"}, "Backend"), gemini)
    self.assertIn("Python", result["required_skills"])
    self.assertTrue(any("AI analysis" in concern for concern in result["analysis_concerns"]))
```

- [ ] **Step 2: Write failing Gemini-object tests**

Mock provider output so `generate_json_object()` accepts a JSON object but raises `GeminiError` for a JSON list and malformed JSON.

- [ ] **Step 3: Run focused tests and confirm failure**

Run: `.\venv\Scripts\python.exe -m unittest tests.test_job_intelligence.JobAnalysisTests tests.test_gemini_client -v`

Expected: missing-interface failures.

- [ ] **Step 4: Implement deterministic-first analysis**

Use bounded regexes and a small canonical alias dictionary in `job_intelligence.py`. Call Gemini only when semantic fields remain incomplete. Validate allowed keys and field types; merge validated semantic lists into deterministic results. Catch `GeminiError`, `TypeError`, and `ValueError`, append an analysis concern, and return usable deterministic data.

Implement `generate_json_object()` as a thin type check around existing `generate_json()`.

- [ ] **Step 5: Run focused tests**

Run: `.\venv\Scripts\python.exe -m unittest tests.test_job_intelligence.JobAnalysisTests tests.test_gemini_client -v`

Expected: all analysis and malformed-response tests pass.

- [ ] **Step 6: Commit**

```powershell
git add job_intelligence.py gemini_client.py tests/test_job_intelligence.py tests/test_gemini_client.py
git commit -m "feat: analyze job requirements safely"
```

### Task 4: Content-Addressed Resume Profiles

**Files:**
- Modify: `resume_profile.py`
- Create: `tests/test_resume_profiles.py`
- Create: `.gitignore`

**Interfaces:**
- Produces: `resume_digest(path: str | Path) -> str`
- Produces: `profile_cache_path(resume_path, cache_dir=PROFILE_CACHE_DIR) -> Path`
- Produces: `validate_resume_profile(value: object) -> dict`
- Preserves: `get_or_build_profile(resume_path, force=False, gemini=None, cache_dir=PROFILE_CACHE_DIR) -> dict`
- Preserves: `profile_for_prompt(profile: dict) -> dict`

- [ ] **Step 1: Write failing cache tests using temporary files**

Use `tempfile.TemporaryDirectory` and a fake Gemini object. Test:

```python
def test_unchanged_resume_reuses_its_own_cache(self):
    first = get_or_build_profile(resume, gemini=fake, cache_dir=cache)
    second = get_or_build_profile(resume, gemini=fake, cache_dir=cache)
    self.assertEqual(first, second)
    self.assertEqual(fake.generate_json_object.call_count, 1)

def test_changing_one_resume_rebuilds_only_that_profile(self):
    # Build A and B, change A bytes, then assert A has two calls/cache IDs and B still has one.
```

Also test matching legacy-profile import, `force=True`, invalid profile shape, and atomic-write cleanup.

- [ ] **Step 2: Run focused tests and confirm failure**

Run: `.\venv\Scripts\python.exe -m unittest tests.test_resume_profiles.ResumeProfileCacheTests -v`

Expected: missing-interface/signature failures.

- [ ] **Step 3: Expand and validate extraction schema**

Add the approved fields to `EXTRACTION_PROMPT`. Call `generate_json_object()`, validate factual types, add `_schema_version`, `_resume_path`, `_resume_sha256`, and `_resume_text`, then write `<sha256>.json` through a same-directory temporary file and `os.replace()`.

Keep the legacy global profile readable only when its absolute path and content identity match. Never overwrite it while processing another resume.

Add `resume_profiles/*.json` to `.gitignore`.

- [ ] **Step 4: Run focused tests**

Run: `.\venv\Scripts\python.exe -m unittest tests.test_resume_profiles.ResumeProfileCacheTests -v`

Expected: all cache tests pass.

- [ ] **Step 5: Commit**

```powershell
git add resume_profile.py tests/test_resume_profiles.py .gitignore
git commit -m "feat: cache a profile for each resume"
```

### Task 5: Profile-Based Resume Ranking

**Files:**
- Modify: `job_intelligence.py`
- Modify: `linkedin_search.py:272-303,424-445,528-535`
- Modify: `tests/test_resume_profiles.py`
- Modify or delete after migration: `test_resume_selection.py`

**Interfaces:**
- Consumes: analyzed jobs from Task 3 and validated profiles from Task 4.
- Produces: `canonical_skill(value: str) -> str`
- Produces: `rank_resumes(job: dict, resume_profiles: dict[str, dict]) -> list[dict]`
- Produces: `load_resume_profiles(config: dict, gemini=None) -> dict[str, dict]`
- Preserves: `choose_resume_path(ctx, config, gemini=None, resume_profiles=None) -> str | None`

- [ ] **Step 1: Write failing ranking tests**

Test exact/alias/partial/missing skills and stable tie-breaking by path. Include:

```python
def test_backend_profile_beats_frontend_profile_for_python_api_job(self):
    ranked = rank_resumes(python_api_job, {"backend.pdf": backend, "frontend.pdf": frontend})
    self.assertEqual(ranked[0]["path"], "backend.pdf")
    self.assertGreater(ranked[0]["score"], ranked[1]["score"])
    self.assertIn("Python", ranked[0]["strong_matches"])

def test_choose_resume_path_keeps_existing_two_argument_call(self):
    selected = choose_resume_path(job, config_with_existing_default)
    self.assertEqual(selected, config_with_existing_default["resume_path"])
```

Assert the `60/15/15/10` component total and reasoning/missing fields.

- [ ] **Step 2: Run focused tests and confirm failure**

Run: `.\venv\Scripts\python.exe -m unittest tests.test_resume_profiles.ResumeRankingTests -v`

Expected: missing ranking interfaces or old filename selector result.

- [ ] **Step 3: Implement ranking through `choose_resume_path()`**

Move skill aliases and profile comparisons into `job_intelligence.py`. Replace the filename-group body of `choose_resume_path()` with profile loading plus `rank_resumes()`. Permit filename scoring only when no validated profile exists, print a compatibility warning, and otherwise use configured existing resume fallback.

Pass the already-created Gemini client and preloaded profiles from the production run. Keep all prior call forms valid.

- [ ] **Step 4: Run resume tests**

Run: `.\venv\Scripts\python.exe -m unittest tests.test_resume_profiles -v`

Expected: cache and ranking tests pass.

Run: `.\venv\Scripts\python.exe test_resume_selection.py`

Expected: pass, or delete this superseded script only after its assertion exists in `tests/test_resume_profiles.py`.

- [ ] **Step 5: Commit**

```powershell
git add job_intelligence.py linkedin_search.py resume_profile.py tests/test_resume_profiles.py test_resume_selection.py
git commit -m "feat: rank resumes against job profiles"
```

### Task 6: Transparent Job Fit Scoring and Ranking

**Files:**
- Modify: `job_intelligence.py`
- Modify: `tests/test_job_intelligence.py`

**Interfaces:**
- Consumes: analyzed job, selected resume profile, matched role profiles, normalized config.
- Produces: `location_points(job: dict, config: dict) -> tuple[int, list[str]]`
- Produces: `score_job(job: dict, resume_profile: dict, role_profiles: list[dict], config: dict) -> dict`
- Produces: `rank_jobs(scored_jobs: list[dict], config: dict) -> list[dict]`

- [ ] **Step 1: Write failing location and score tests**

Cover exact points for Karachi `10`, remote Pakistan `9`, Pakistan `7`, explicitly remote-compatible foreign `5`, incompatible `0`, and unknown `0` plus a concern.

Add exact component assertions:

```python
def test_score_is_sum_of_visible_components(self):
    result = score_job(job, profile, roles, config)
    self.assertEqual(result["score"], sum(c["earned"] for c in result["components"].values()))
    self.assertEqual(sum(c["maximum"] for c in result["components"].values()), 100)

def test_unknown_required_skills_do_not_receive_assumed_points(self):
    result = score_job(job_without_requirements, profile, roles, config)
    self.assertEqual(result["components"]["required_skills"]["earned"], 0)
    self.assertIn("requirements unavailable", result["concerns"])
```

Also test A/B/C/D boundaries, experience rules, excluded companies, preferred-company tie-break only, priority, recency, and applicant-count ordering.

- [ ] **Step 2: Run focused tests and confirm failure**

Run: `.\venv\Scripts\python.exe -m unittest tests.test_job_intelligence.JobFitScoringTests -v`

Expected: missing scoring interfaces.

- [ ] **Step 3: Implement component scoring and stable ranking**

Return every component with `earned`, `maximum`, and `evidence`. Clamp the rounded total to `0..100`. Build `strong_matches`, `partial_matches`, missing lists, concerns, deterministic reasoning, tier, and recommendation threshold. Filter excluded companies before ranking and apply preferred companies only in the sort key.

- [ ] **Step 4: Run focused tests**

Run: `.\venv\Scripts\python.exe -m unittest tests.test_job_intelligence.JobFitScoringTests -v`

Expected: all scoring tests pass.

- [ ] **Step 5: Commit**

```powershell
git add job_intelligence.py tests/test_job_intelligence.py
git commit -m "feat: add transparent job fit scoring"
```

### Task 7: Collect, Deduplicate, Rank, Then Apply

**Files:**
- Modify: `linkedin_search.py:424-633`
- Modify: `tests/test_job_intelligence.py`

**Interfaces:**
- Consumes: all interfaces from Tasks 1–6 and the existing Easy Apply functions.
- Produces: `_collect_current_search(page, role_profile: dict, config: dict) -> list[dict]`
- Produces: `discover_jobs(page, config: dict) -> list[dict]`
- Produces: `prepare_ranked_jobs(jobs: list[dict], resume_profiles: dict[str, dict], config: dict, gemini=None) -> list[dict]`
- Preserves: `run(config: dict) -> int`
- Preserves: `main() -> None`

- [ ] **Step 1: Write failing orchestration tests with mocks**

Patch `_collect_current_search`, `analyze_job`, and `choose_resume_path`; do not construct Playwright objects. Assert:

```python
def test_discovery_deduplicates_across_profiles_before_analysis(self):
    # Two profile searches return the same ID.
    jobs = discover_jobs(fake_page, config)
    self.assertEqual(len(jobs), 1)
    self.assertEqual(jobs[0]["matched_roles"], ["Backend Engineer", "AI Software Engineer"])

def test_ranked_pipeline_uses_selected_resume_profile(self):
    ranked = prepare_ranked_jobs([job], profiles, config, gemini=None)
    self.assertEqual(ranked[0]["selected_resume"], "backend.pdf")
    self.assertEqual(ranked[0]["fit"]["score"], expected_score)

def test_submitted_tracking_depends_on_apply_result(self):
    # Patch apply_to_current_job to False and assert save_applied_id is not called.
```

Also assert disabled profiles are not searched, links.txt remains supported as a compatibility discovery source, and global ordering happens before the application loop.

- [ ] **Step 2: Run focused tests and confirm failure**

Run: `.\venv\Scripts\python.exe -m unittest tests.test_job_intelligence.SearchOrchestrationTests -v`

Expected: missing orchestration interfaces or immediate per-role processing.

- [ ] **Step 3: Refactor orchestration without rewriting DOM helpers**

Reuse `open_jobs_search()`, `hydrate_cards()`, `job_cards()`, `applicant_count()`, and `job_context()`. Extend `job_context()` only with structured top-card fields for employment type, workplace type, seniority, posting text, and application method. Collect raw job dictionaries for every enabled profile/page, deduplicate, analyze once, rank resumes, score, and sort before the existing filtering/application loop.

Build each search query from the profile's keywords joined with ` OR `, falling back to its name when the keyword list is empty. Do not interpolate profile metadata into the query.

When applying a ranked job, navigate to its canonical URL, re-read the live context, pass the selected resume/profile to `apply_to_current_job()`, and call `save_applied_id()` only when that function returns `True`. Use `matched_roles` in skip records instead of indexing `config["role"]`.

Keep positional role/applicant overrides by converting them into one normalized target profile. Preserve links.txt behavior without treating it as proof that every configured role matched.

- [ ] **Step 4: Run all unit tests and the legacy check**

Run: `.\venv\Scripts\python.exe -m unittest discover -s tests -v`

Expected: all tests pass without network, browser, or Gemini calls.

Run: `.\venv\Scripts\python.exe test_resume_selection.py`

Expected: pass if retained.

- [ ] **Step 5: Commit**

```powershell
git add linkedin_search.py tests/test_job_intelligence.py
git commit -m "refactor: rank jobs before application"
```

### Task 8: Final Verification

**Files:**
- Verify only; modify task-owned files only if a failing check exposes a defect.

**Interfaces:**
- Consumes: completed Tasks 1–7.
- Produces: a verified foundation with no live side effects.

- [ ] **Step 1: Run the complete offline test suite**

Run: `.\venv\Scripts\python.exe -m unittest discover -s tests -v`

Expected: all tests pass.

- [ ] **Step 2: Run required compilation checks**

Run: `.\venv\Scripts\python.exe -m py_compile linkedin_search.py easy_apply.py qa_store.py resume_profile.py gemini_client.py linkedin_login.py job_intelligence.py app.py apply_one.py`

Expected: exit code `0` with no output.

- [ ] **Step 3: Confirm generated and user data are untouched**

Run: `git status --short`

Expected: no changes to resume PDFs, `applied_jobs.json`, `not_targeted_jobs.json`, `not_targeted_jobs.txt`, `qa_cache.json`, `resume_profile.json`, or the user's untracked `PLAN.md`.

- [ ] **Step 4: Review the final diff against scope**

Run: `git diff e3a6149..HEAD --stat` and `git diff e3a6149..HEAD -- easy_apply.py qa_store.py app.py templates static`

Expected: only the planned files changed; the second command has no output.

- [ ] **Step 5: Commit verification-only fixes if needed**

If verification required a code fix, commit only that fix with a specific message. Otherwise, do not create an empty commit.
