import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import date
from pathlib import Path
from unittest.mock import Mock
from unittest.mock import patch

from job_intelligence import (
    analyze_job,
    canonical_job_url,
    deduplicate_jobs,
    deterministic_job_analysis,
    enabled_role_profiles,
    job_key,
    location_points,
    merge_jobs,
    normalize_config,
    normalize_job,
    recency_filter,
    rank_jobs,
    score_job,
)
from linkedin_search import (
    _classic_search_url,
    _collect_current_search,
    apply_ranked_jobs,
    discover_jobs,
    job_id,
    load_applied_ids,
    load_not_targeted,
    load_scanned_jobs,
    load_retryable_jobs,
    load_resume_profiles,
    prepare_ranked_jobs,
    posting_age_days,
    save_applied_id,
    save_not_targeted,
    save_scanned_jobs,
    update_job_status,
)


class JobIntelligenceConfigTests(unittest.TestCase):
    def test_legacy_role_becomes_target_profile(self):
        config = normalize_config({"role": "Backend Developer"})

        self.assertEqual(
            [profile["name"] for profile in enabled_role_profiles(config)],
            ["Backend Developer"],
        )
        self.assertEqual(config["posted_within_days"], 7)

    def test_legacy_roles_and_past_24_hours_are_supported(self):
        config = normalize_config(
            {"roles": ["Backend", "Data Engineer"], "past_24_hours": True}
        )

        self.assertEqual(len(enabled_role_profiles(config)), 2)
        self.assertEqual(config["posted_within_days"], 1)

    def test_explicit_search_window_wins_over_legacy_flag(self):
        config = normalize_config(
            {"role": "Backend", "past_24_hours": True, "posted_within_days": 7}
        )

        self.assertEqual(config["posted_within_days"], 7)

    def test_invalid_weight_total_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "total 100"):
            normalize_config(
                {"role": "Backend", "fit_score_weights": {"role_title": 10}}
            )

    def test_search_windows_map_to_linkedin_filters(self):
        self.assertEqual(recency_filter(1), "r86400")
        self.assertEqual(recency_filter(3), "r259200")
        self.assertEqual(recency_filter(7), "r604800")

    def test_invalid_search_window_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "posted_within_days"):
            normalize_config({"role": "Backend", "posted_within_days": 2})

    def test_invalid_role_fields_are_rejected(self):
        invalid_profiles = [
            ({"name": "", "priority": 1, "keywords": []}, "name"),
            ({"name": "Backend", "priority": 4, "keywords": []}, "priority"),
            ({"name": "Backend", "priority": 1, "keywords": "python"}, "keywords"),
            (
                {
                    "name": "Backend",
                    "priority": 1,
                    "keywords": [],
                    "minimum_fit_score": 101,
                },
                "minimum_fit_score",
            ),
        ]

        for profile, message in invalid_profiles:
            with self.subTest(profile=profile):
                with self.assertRaisesRegex(ValueError, message):
                    normalize_config({"target_roles": [profile]})

    def test_disabled_invalid_profile_is_ignored(self):
        config = normalize_config(
            {
                "role": "Fallback",
                "target_roles": [
                    {"name": "", "priority": 99, "keywords": "bad", "enabled": False},
                    {
                        "name": "Backend",
                        "priority": 1,
                        "keywords": ["python"],
                        "minimum_fit_score": 72,
                        "enabled": True,
                    },
                ],
            }
        )

        self.assertEqual([p["name"] for p in enabled_role_profiles(config)], ["Backend"])


class JobNormalizationTests(unittest.TestCase):
    def test_linkedin_id_is_preferred_and_url_is_canonical(self):
        job = normalize_job(
            {
                "url": "https://www.linkedin.com/jobs/search/?currentJobId=123&trk=x",
                "title": "Backend Engineer",
            },
            "Backend",
        )

        self.assertEqual(job["job_id"], "123")
        self.assertEqual(job["url"], "https://www.linkedin.com/jobs/view/123")
        self.assertEqual(job_key(job), "linkedin:123")

    def test_canonical_url_strips_tracking_parameters(self):
        url = canonical_job_url(
            "https://www.linkedin.com/jobs/view/backend-engineer-at-acme-123/?trk=public&refId=x"
        )

        self.assertEqual(
            url,
            "https://www.linkedin.com/jobs/view/123",
        )

    def test_detail_key_normalizes_company_title_and_location(self):
        job = normalize_job(
            {
                "title": " Backend Engineer ",
                "company": "ACME, Inc.",
                "location": "Karachi, Pakistan",
            },
            "Backend",
        )

        self.assertEqual(
            job_key(job),
            "details:acme inc|backend engineer|karachi pakistan",
        )

    def test_duplicate_observations_merge_roles_and_richer_fields(self):
        jobs = deduplicate_jobs(
            [
                normalize_job(
                    {
                        "url": "https://www.linkedin.com/jobs/view/123",
                        "title": "Backend Engineer",
                        "applicant_count": 45,
                    },
                    "Backend Engineer",
                ),
                normalize_job(
                    {
                        "url": "https://www.linkedin.com/jobs/search/?currentJobId=123",
                        "description": "Build APIs",
                        "applicant_count": 42,
                        "required_skills": ["Python"],
                    },
                    "AI Software Engineer",
                ),
            ]
        )

        self.assertEqual(len(jobs), 1)
        self.assertEqual(
            jobs[0]["matched_roles"],
            ["Backend Engineer", "AI Software Engineer"],
        )
        self.assertEqual(jobs[0]["description"], "Build APIs")
        self.assertEqual(jobs[0]["applicant_count"], 42)
        self.assertEqual(jobs[0]["required_skills"], ["Python"])

    def test_merge_unions_lists_without_reordering(self):
        first = normalize_job(
            {"url": "https://example.com/job", "required_skills": ["Python"]},
            "Backend",
        )
        second = normalize_job(
            {
                "url": "https://example.com/job?source=feed",
                "required_skills": ["Django", "Python"],
                "responsibilities": ["Build APIs"],
            },
            "Python",
        )

        merged = merge_jobs(first, second)

        self.assertEqual(merged["required_skills"], ["Python", "Django"])
        self.assertEqual(merged["responsibilities"], ["Build APIs"])
        self.assertEqual(merged["matched_roles"], ["Backend", "Python"])

    def test_relative_posting_age_becomes_iso_date(self):
        job = normalize_job(
            {"posting_date": "5 days ago"},
            "Backend",
            today=date(2026, 10, 7),
        )

        self.assertEqual(job["posting_date"], "2026-10-02")

    def test_normalized_job_contains_complete_schema(self):
        job = normalize_job({}, "Backend")

        self.assertEqual(
            set(job),
            {
                "job_id",
                "url",
                "title",
                "company",
                "location",
                "employment_type",
                "workplace_type",
                "seniority",
                "posting_date",
                "applicant_count",
                "description",
                "required_skills",
                "preferred_skills",
                "years_experience",
                "education_requirement",
                "responsibilities",
                "application_method",
                "matched_roles",
            },
        )


class JobAnalysisTests(unittest.TestCase):
    def test_extracts_deterministic_requirements(self):
        job = normalize_job(
            {
                "title": "Senior Backend Engineer",
                "description": (
                    "Requirements:\n"
                    "3+ years of experience with Python, Django, and PostgreSQL.\n"
                    "Bachelor's degree in Computer Science.\n"
                    "Preferred: Docker and AWS.\n"
                    "Responsibilities:\nBuild REST APIs and database services."
                ),
            },
            "Backend Engineer",
        )

        result = deterministic_job_analysis(job)

        self.assertEqual(result["years_experience"], 3)
        self.assertEqual(result["seniority"], "Senior")
        self.assertEqual(result["required_skills"], ["Python", "Django", "PostgreSQL"])
        self.assertEqual(result["preferred_skills"], ["Docker", "AWS"])
        self.assertEqual(result["education_requirement"], "Bachelor's degree")
        self.assertEqual(result["responsibilities"], ["Build REST APIs and database services."])

    def test_description_remote_word_does_not_set_workplace_type(self):
        job = normalize_job(
            {
                "location": "Lahore, Pakistan",
                "description": "Collaborate with remote teams around the world.",
            },
            "Backend",
        )

        self.assertIsNone(deterministic_job_analysis(job)["workplace_type"])

    def test_malformed_ai_analysis_keeps_deterministic_data(self):
        gemini = Mock()
        gemini.generate_json_object.return_value = {"required_skills": "Python"}
        job = normalize_job(
            {"description": "Requirements: Python."},
            "Backend",
        )

        result = analyze_job(job, gemini)

        self.assertIn("Python", result["required_skills"])
        self.assertTrue(
            any("AI analysis" in concern for concern in result["analysis_concerns"])
        )

    def test_valid_ai_analysis_only_fills_semantic_fields(self):
        gemini = Mock()
        gemini.generate_json_object.return_value = {
            "required_skills": ["FastAPI"],
            "preferred_skills": ["Docker"],
            "years_experience": 2,
            "education_requirement": None,
            "responsibilities": ["Build APIs"],
            "employment_type": "Full-time",
            "workplace_type": "Remote",
            "seniority": "Mid level",
        }
        job = normalize_job(
            {"job_id": "123", "title": "API Engineer", "description": "Build services.", "workplace_type": "Remote"},
            "Backend",
        )

        result = analyze_job(job, gemini)

        self.assertEqual(result["job_id"], "123")
        self.assertEqual(result["required_skills"], ["FastAPI"])
        self.assertEqual(result["workplace_type"], "Remote")
        self.assertEqual(result["analysis_concerns"], [])

    def test_ai_cannot_infer_remote_workplace_from_description(self):
        gemini = Mock()
        gemini.generate_json_object.return_value = {
            "required_skills": ["Python"], "preferred_skills": [],
            "years_experience": None, "education_requirement": None,
            "responsibilities": [], "employment_type": None,
            "workplace_type": "Remote", "seniority": None,
        }
        job = normalize_job(
            {"location": "Lahore, Pakistan", "description": "Collaborate with remote teams."},
            "Backend",
        )

        self.assertIsNone(analyze_job(job, gemini)["workplace_type"])


class JobFitScoringTests(unittest.TestCase):
    def setUp(self):
        self.config = normalize_config({"role": "Backend Engineer"})
        self.roles = enabled_role_profiles(self.config)
        self.profile = {
            "skills": ["Python", "Django", "REST API", "PostgreSQL", "Docker", "AWS", "LangChain"],
            "skill_years": {"Python": 3, "Django": 2},
            "roles": ["Backend Engineer"],
            "total_years_experience": 3,
            "target_role_categories": ["backend", "ai"],
        }

    def test_location_points_follow_requested_order(self):
        cases = [
            ({"location": "Karachi, Pakistan", "workplace_type": "On-site"}, 10),
            ({"location": "Pakistan", "workplace_type": "Remote"}, 10),
            ({"location": "Islamabad, Pakistan", "workplace_type": "Hybrid"}, 7),
            ({"location": "Dubai, UAE", "workplace_type": "Remote"}, 10),
            ({"location": "Dubai, UAE", "workplace_type": "On-site"}, 0),
        ]

        for job, expected in cases:
            with self.subTest(job=job):
                self.assertEqual(location_points(job, self.config)[0], expected)

    def test_unknown_location_scores_zero_and_adds_concern(self):
        points, concerns = location_points({"location": "", "workplace_type": None}, self.config)

        self.assertEqual(points, 0)
        self.assertIn("location/workplace unavailable", concerns)

    def test_score_is_sum_of_visible_components(self):
        job = {
            "title": "AI Backend Engineer",
            "company": "Example",
            "location": "Karachi, Pakistan",
            "workplace_type": "Hybrid",
            "required_skills": ["Python", "Django", "REST APIs"],
            "preferred_skills": ["Docker"],
            "years_experience": 3,
            "seniority": "Mid level",
            "description": "Build AI APIs with PostgreSQL on AWS",
            "responsibilities": ["Build database-backed APIs"],
            "matched_roles": ["Backend Engineer"],
        }

        result = score_job(job, self.profile, self.roles, self.config)

        self.assertEqual(
            result["score"],
            sum(component["earned"] for component in result["components"].values()),
        )
        self.assertEqual(
            sum(component["maximum"] for component in result["components"].values()),
            100,
        )
        self.assertEqual(result["tier"], "A")

    def test_unknown_required_skills_do_not_receive_assumed_points(self):
        job = {
            "title": "Backend Engineer",
            "company": "Example",
            "location": "Karachi, Pakistan",
            "workplace_type": "On-site",
            "required_skills": [],
            "preferred_skills": [],
            "years_experience": None,
            "description": "",
            "responsibilities": [],
            "matched_roles": ["Backend Engineer"],
        }

        result = score_job(job, self.profile, self.roles, self.config)

        self.assertEqual(result["components"]["required_skills"]["earned"], 0)
        self.assertIn("requirements unavailable", result["concerns"])

    def test_matched_role_does_not_make_an_unrelated_title_match(self):
        result = score_job(
            {"title": "Accountant", "matched_roles": ["Backend Engineer"],
             "required_skills": [], "preferred_skills": [],
             "description": "", "responsibilities": []},
            self.profile, self.roles, self.config,
        )

        self.assertEqual(result["components"]["role_title"]["earned"], 0)

    def test_thresholds_produce_b_and_c_tiers(self):
        job = {
            "title": "Backend Engineer",
            "company": "Example",
            "location": "Dubai, UAE",
            "workplace_type": "On-site",
            "required_skills": ["Python"],
            "preferred_skills": [],
            "years_experience": None,
            "description": "Build backend APIs",
            "responsibilities": [],
            "matched_roles": ["Backend Engineer"],
        }
        score = score_job(job, self.profile, self.roles, self.config)["score"]
        b_config = normalize_config(
            {
                "target_roles": [{"name": "Backend Engineer", "minimum_fit_score": score}],
                "minimum_fit_score": score,
                "stretch_fit_score": max(0, score - 10),
            }
        )
        c_config = normalize_config(
            {
                "target_roles": [{"name": "Backend Engineer", "minimum_fit_score": min(100, score + 1)}],
                "minimum_fit_score": min(100, score + 1),
                "stretch_fit_score": score,
            }
        )

        self.assertLess(score, 80)
        self.assertEqual(score_job(job, self.profile, enabled_role_profiles(b_config), b_config)["tier"], "B")
        self.assertEqual(score_job(job, self.profile, enabled_role_profiles(c_config), c_config)["tier"], "C")

    def test_preferred_company_breaks_tie_without_changing_score(self):
        config = {**self.config, "preferred_companies": ["Preferred Co"]}
        jobs = [
            {"job": {"company": "Other Co", "posting_date": "2026-10-07", "applicant_count": 10}, "fit": {"score": 70, "tier": "B"}, "role_priority": 1},
            {"job": {"company": "Preferred Co", "posting_date": "2026-10-07", "applicant_count": 10}, "fit": {"score": 70, "tier": "B"}, "role_priority": 1},
        ]

        ranked = rank_jobs(jobs, config)

        self.assertEqual(ranked[0]["job"]["company"], "Preferred Co")
        self.assertEqual([item["fit"]["score"] for item in ranked], [70, 70])

    def test_excluded_company_is_removed(self):
        config = {**self.config, "excluded_companies": ["Blocked Co"]}
        jobs = [
            {"job": {"company": "Blocked Co"}, "fit": {"score": 100, "tier": "A"}, "role_priority": 1},
            {"job": {"company": "Allowed Co"}, "fit": {"score": 60, "tier": "C"}, "role_priority": 3},
        ]

        self.assertEqual(
            [item["job"]["company"] for item in rank_jobs(jobs, config)],
            ["Allowed Co"],
        )


class SearchOrchestrationTests(unittest.TestCase):
    def test_posting_age_parses_known_dates_and_preserves_unknown(self):
        today = date(2026, 10, 10)

        self.assertEqual(posting_age_days("2026-09-10", today), 30)
        self.assertEqual(posting_age_days("5 weeks ago", today), 35)
        self.assertEqual(posting_age_days("23 hours ago", today), 0)
        self.assertIsNone(posting_age_days(None, today))

    def test_applied_job_is_saved_as_a_structured_record(self):
        job = {
            "job_id": "123", "title": "Backend Engineer", "company": "Example",
            "location": "Karachi", "url": "https://www.linkedin.com/jobs/view/123",
        }
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "jobs.db"
            save_applied_id("123", job, "Backend Engineer", path)
            records = load_scanned_jobs("applied", path)
            applied_ids = load_applied_ids(path)

        by_id = {record["job_id"]: record for record in records}
        self.assertEqual(applied_ids, {"123"})
        self.assertEqual(by_id["123"]["title"], "Backend Engineer")

    def test_not_targeted_record_has_explicit_status(self):
        job = {"job_id": "123", "title": "Backend Engineer", "company": "Example", "location": "Karachi"}
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "jobs.db"
            save_not_targeted(job, "no Easy Apply button", "Backend Engineer", path)
            records = load_not_targeted(path)

        self.assertEqual(records["linkedin:123"]["reason"], "no Easy Apply button")

    def test_saved_sdui_link_becomes_unpinned_classic_search(self):
        url = _classic_search_url(
            "https://www.linkedin.com/jobs/search-results/?currentJobId=123&keywords=Backend&"
            "origin=PREFERENCES_LANDING&referralSearchId=abc&f_AL=true"
        )

        self.assertEqual(
            url,
            "https://www.linkedin.com/jobs/search/?keywords=Backend&f_AL=true",
        )

    def test_job_id_accepts_normalized_job_and_canonical_url(self):
        self.assertEqual(
            job_id({"job_id": "123", "url": "https://www.linkedin.com/jobs/view/123"}),
            "123",
        )

    def test_discovery_deduplicates_across_profiles_before_analysis(self):
        config = normalize_config(
            {
                "target_roles": [
                    {"name": "Backend Engineer", "priority": 1, "keywords": ["backend"]},
                    {"name": "AI Software Engineer", "priority": 2, "keywords": ["ai"]},
                ],
                "max_search_pages": 1,
            }
        )
        observations = [
            [normalize_job({"job_id": "123", "title": "Backend Engineer"}, "Backend Engineer")],
            [normalize_job({"job_id": "123", "description": "Build AI APIs"}, "AI Software Engineer")],
        ]

        with (
            patch("linkedin_search.load_links", return_value=[]),
            patch("linkedin_search._collect_current_search", side_effect=observations) as collect,
        ):
            jobs = discover_jobs(Mock(), config)

        self.assertEqual(collect.call_count, 2)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(
            jobs[0]["matched_roles"],
            ["Backend Engineer", "AI Software Engineer"],
        )

    def test_disabled_profiles_are_not_searched(self):
        config = normalize_config(
            {
                "target_roles": [
                    {"name": "Backend", "priority": 1, "keywords": [], "enabled": True},
                    {"name": "Frontend", "priority": 1, "keywords": [], "enabled": False},
                ],
                "max_search_pages": 1,
            }
        )

        with (
            patch("linkedin_search.load_links", return_value=[]),
            patch("linkedin_search._collect_current_search", return_value=[]) as collect,
        ):
            discover_jobs(Mock(), config)

        self.assertEqual(collect.call_count, 1)
        self.assertEqual(collect.call_args.args[1]["name"], "Backend")

    def test_links_file_remains_a_discovery_source(self):
        config = normalize_config({"role": "Backend", "max_search_pages": 1})

        with (
            patch("linkedin_search.load_links", return_value=["https://linkedin.test/search"]),
            patch("linkedin_search._collect_current_search", return_value=[]) as collect,
        ):
            discover_jobs(Mock(), config)

        profile = collect.call_args.args[1]
        self.assertEqual(profile["name"], "roles from links.txt")
        self.assertEqual(profile["target_url"], "https://linkedin.test/search")

    def test_legacy_config_keeps_ten_search_page_default(self):
        config = normalize_config({"role": "Backend"})
        observation = [
            normalize_job({"job_id": str(index)}, "Backend") for index in range(25)
        ]

        with (
            patch("linkedin_search.load_links", return_value=[]),
            patch("linkedin_search._collect_current_search", return_value=observation) as collect,
        ):
            discover_jobs(Mock(), config)

        self.assertEqual(collect.call_count, 10)

    def test_discovery_stops_after_partial_page(self):
        config = normalize_config({"role": "Backend", "max_search_pages": 10})
        observation = [normalize_job({"job_id": "123"}, "Backend")]
        with (
            patch("linkedin_search.load_links", return_value=[]),
            patch("linkedin_search._collect_current_search", return_value=observation) as collect,
        ):
            discover_jobs(Mock(), config)

        self.assertEqual(collect.call_count, 1)

    def test_transient_linkedin_results_error_reloads_once(self):
        page = Mock()
        page.locator.return_value.inner_text.return_value = (
            "Unfortunately, things aren’t loading. We're having issues loading your results."
        )
        config = normalize_config({"role": "Backend", "page_load_wait_ms": 0})
        profile = {"name": "Backend", "keywords": ["Backend"]}
        with (
            patch("linkedin_search.open_jobs_search"),
            patch("linkedin_search.hydrate_cards", side_effect=[0, 0]) as hydrate,
        ):
            jobs = _collect_current_search(page, profile, config)

        self.assertEqual(jobs, [])
        self.assertEqual(hydrate.call_count, 2)
        page.reload.assert_called_once()

    def test_ranked_pipeline_uses_selected_resume_profile(self):
        config = normalize_config({"role": "Backend Engineer"})
        backend = {
            "skills": ["Python", "Django", "REST API"],
            "skill_years": {"Python": 3},
            "roles": ["Backend Engineer"],
            "total_years_experience": 3,
            "target_role_categories": ["backend"],
        }
        frontend = {
            "skills": ["React"],
            "skill_years": {"React": 2},
            "roles": ["Frontend Engineer"],
            "total_years_experience": 2,
            "target_role_categories": ["frontend"],
        }
        job = normalize_job(
            {
                "job_id": "123",
                "title": "Backend Engineer",
                "location": "Karachi, Pakistan",
                "workplace_type": "On-site",
                "required_skills": ["Python", "Django", "REST APIs"],
                "description": "Build backend APIs",
            },
            "Backend Engineer",
        )

        ranked = prepare_ranked_jobs(
            [job],
            {"backend.pdf": backend, "frontend.pdf": frontend},
            config,
        )

        self.assertEqual(ranked[0]["selected_resume"], "backend.pdf")
        self.assertGreater(ranked[0]["fit"]["score"], 0)
        self.assertEqual(ranked[0]["resume_profile"], backend)

    def test_configured_resume_outside_resumes_directory_is_loaded(self):
        with tempfile.TemporaryDirectory() as root:
            resume = Path(root) / "resume.txt"
            resume.write_text("resume", encoding="utf-8")
            profile = {"skills": ["Python"], "skill_years": {}}
            with patch("linkedin_search.get_or_build_profile", return_value=profile) as build:
                profiles = load_resume_profiles({"resume_path": str(resume)}, gemini=Mock())

        self.assertEqual(profiles[str(resume)], profile)
        self.assertIn(unittest.mock.call(resume, gemini=unittest.mock.ANY), build.call_args_list)

    def test_submitted_tracking_depends_on_apply_result(self):
        page = Mock()
        ranked = [
            {
                "job": {
                    "job_id": "123",
                    "url": "https://www.linkedin.com/jobs/view/123",
                    "title": "Backend Engineer",
                    "company": "Example",
                    "location": "Karachi, Pakistan",
                    "applicant_count": 10,
                    "matched_roles": ["Backend Engineer"],
                },
                "selected_resume": "backend.pdf",
                "resume_profile": {"skills": ["Python"]},
                "fit": {"score": 90, "tier": "A"},
            }
        ]

        with (
            patch("linkedin_search.job_context", return_value={}),
            patch("linkedin_search.applicant_count", return_value=None),
            patch("linkedin_search.already_applied", return_value=False),
            patch("linkedin_search.click_easy_apply", return_value=True),
            patch("linkedin_search.apply_to_current_job", return_value=False),
            patch("linkedin_search.posting_age_days", return_value=0),
            patch("linkedin_search.save_applied_id") as save,
            patch("linkedin_search.save_not_targeted"),
            patch("linkedin_search.update_job_status"),
        ):
            applied, skipped = apply_ranked_jobs(
                page,
                ranked,
                normalize_config({"role": "Backend Engineer", "max_applications": 5}),
                Mock(),
                Mock(),
                set(),
            )

        self.assertEqual((applied, skipped), (0, 1))
        save.assert_not_called()

    def test_applicant_count_does_not_block_applying(self):
        page = Mock()
        ranked = [{
            "job": {"job_id": "123", "url": "https://www.linkedin.com/jobs/view/123",
                    "title": "Backend Engineer", "company": "Example", "location": "Karachi",
                    "applicant_count": 10, "matched_roles": ["Backend Engineer"]},
            "selected_resume": "backend.pdf", "resume_profile": {"skills": ["Python"]},
            "fit": {"score": 90, "tier": "A"},
        }]
        with (
            patch("linkedin_search.job_context", return_value={"title": "Backend Engineer"}),
            patch("linkedin_search.applicant_count", return_value=101),
            patch("linkedin_search.already_applied", return_value=False),
            patch("linkedin_search.click_easy_apply", return_value=True) as click,
            patch("linkedin_search.apply_to_current_job", return_value=True),
            patch("linkedin_search.posting_age_days", return_value=0),
            patch("linkedin_search.load_not_targeted", return_value={
                "linkedin:123": {"reason": "over 100-applicant cap"}
            }),
            patch("linkedin_search.save_applied_id"),
            patch("linkedin_search.save_not_targeted") as save,
            patch("linkedin_search.update_job_status"),
        ):
            applied, skipped = apply_ranked_jobs(
                page, ranked,
                normalize_config({"role": "Backend Engineer", "applicants": 100}),
                Mock(), Mock(), set(),
            )

        self.assertEqual((applied, skipped), (1, 0))
        click.assert_called_once()
        save.assert_not_called()

    def test_scanned_jobs_are_upserted_in_sqlite(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "jobs.db"
            first = normalize_job(
                {"job_id": "123", "title": "Backend Engineer", "company": "Example"},
                "Backend Engineer",
            )
            save_scanned_jobs([first], path)
            save_scanned_jobs(
                [{**first, "applicant_count": 250, "fit_score": 72, "fit_tier": "B"}],
                path,
            )
            update_job_status(first, "skipped", "no Easy Apply button", path)
            retryable = normalize_job({"job_id": "456", "title": "API Engineer"}, "Backend Engineer")
            update_job_status(retryable, "skipped", "fit score 49 below threshold", path)
            discovered = normalize_job({"job_id": "789", "title": "Python Engineer"}, "Backend Engineer")
            save_scanned_jobs([discovered], path)

            with closing(sqlite3.connect(path)) as database:
                rows = database.execute(
                    """SELECT job_id, applicant_count, job_json, status, status_reason,
                    fit_score, fit_tier
                    FROM scanned_jobs"""
                ).fetchall()
                columns = {row[1] for row in database.execute("PRAGMA table_info(scanned_jobs)")}
            skipped_jobs = load_scanned_jobs("skipped", path)
            discovered_jobs = load_scanned_jobs("discovered", path)
            retryable_jobs = load_retryable_jobs(path)

        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0][:2], ("123", 250))
        self.assertEqual(json.loads(rows[0][2])["matched_roles"], ["Backend Engineer"])
        self.assertEqual(rows[0][3:5], ("skipped", "no Easy Apply button"))
        self.assertEqual(rows[0][5:], (72, "B"))
        self.assertEqual(skipped_jobs[0]["job_id"], "123")
        self.assertEqual([job["job_id"] for job in discovered_jobs], ["789"])
        self.assertEqual({job["job_id"] for job in retryable_jobs}, {"456", "789"})
        self.assertIn("fit_score", columns)
        self.assertEqual([job["job_id"] for job in retryable_jobs[:2]], ["789", "456"])


if __name__ == "__main__":
    unittest.main()
