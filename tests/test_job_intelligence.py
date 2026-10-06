import unittest
from datetime import date
from unittest.mock import Mock

from job_intelligence import (
    analyze_job,
    canonical_job_url,
    deduplicate_jobs,
    deterministic_job_analysis,
    enabled_role_profiles,
    job_key,
    merge_jobs,
    normalize_config,
    normalize_job,
    recency_filter,
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
            {"job_id": "123", "title": "API Engineer", "description": "Build services."},
            "Backend",
        )

        result = analyze_job(job, gemini)

        self.assertEqual(result["job_id"], "123")
        self.assertEqual(result["required_skills"], ["FastAPI"])
        self.assertEqual(result["workplace_type"], "Remote")
        self.assertEqual(result["analysis_concerns"], [])


if __name__ == "__main__":
    unittest.main()
