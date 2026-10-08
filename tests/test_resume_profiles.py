import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from job_intelligence import canonical_skill, rank_resumes
from linkedin_search import choose_resume_path
from resume_profile import (
    get_or_build_profile,
    profile_cache_path,
    resume_digest,
    validate_resume_profile,
)


def factual_profile(headline="Backend Engineer"):
    return {
        "full_name": "Candidate",
        "headline": headline,
        "skills": ["Python", "Django"],
        "skill_years": {"Python": 3},
        "roles": ["Backend Engineer"],
        "experience": [{"title": "Backend Engineer", "years": 3}],
        "domains": ["APIs"],
        "projects": [{"name": "API Platform", "skills": ["Python"]}],
        "education": [{"degree": "BS"}],
        "certifications": [],
        "keywords": ["backend", "python"],
        "target_role_categories": ["backend"],
    }


class ResumeProfileCacheTests(unittest.TestCase):
    def test_unchanged_resume_reuses_its_own_cache(self):
        with tempfile.TemporaryDirectory() as root:
            resume = Path(root) / "backend.txt"
            cache = Path(root) / "cache"
            resume.write_text("Python backend resume", encoding="utf-8")
            fake = Mock()
            fake.generate_json_object.return_value = factual_profile()

            first = get_or_build_profile(resume, gemini=fake, cache_dir=cache)
            second = get_or_build_profile(resume, gemini=fake, cache_dir=cache)

            self.assertEqual(first, second)
            self.assertEqual(fake.generate_json_object.call_count, 1)
            self.assertTrue(profile_cache_path(resume, cache).exists())
            self.assertEqual(first["_resume_sha256"], resume_digest(resume))

    def test_changing_one_resume_rebuilds_only_that_profile(self):
        with tempfile.TemporaryDirectory() as root:
            cache = Path(root) / "cache"
            first_resume = Path(root) / "backend.txt"
            second_resume = Path(root) / "data.txt"
            first_resume.write_text("Python", encoding="utf-8")
            second_resume.write_text("SQL", encoding="utf-8")
            fake = Mock()
            fake.generate_json_object.return_value = factual_profile()

            get_or_build_profile(first_resume, gemini=fake, cache_dir=cache)
            second_before = get_or_build_profile(second_resume, gemini=fake, cache_dir=cache)
            first_resume.write_text("Python and Django", encoding="utf-8")
            get_or_build_profile(first_resume, gemini=fake, cache_dir=cache)
            second_after = get_or_build_profile(second_resume, gemini=fake, cache_dir=cache)

            self.assertEqual(fake.generate_json_object.call_count, 3)
            self.assertEqual(second_before, second_after)
            self.assertEqual(len(list(cache.glob("*.json"))), 3)

    def test_force_rebuilds_only_selected_cache_entry(self):
        with tempfile.TemporaryDirectory() as root:
            resume = Path(root) / "backend.txt"
            cache = Path(root) / "cache"
            resume.write_text("Python", encoding="utf-8")
            fake = Mock()
            fake.generate_json_object.return_value = factual_profile()

            get_or_build_profile(resume, gemini=fake, cache_dir=cache)
            get_or_build_profile(resume, force=True, gemini=fake, cache_dir=cache)

            self.assertEqual(fake.generate_json_object.call_count, 2)
            self.assertEqual(len(list(cache.glob("*.json"))), 1)

    def test_matching_legacy_profile_is_imported_without_ai_call(self):
        with tempfile.TemporaryDirectory() as root:
            resume = Path(root) / "backend.txt"
            cache = Path(root) / "cache"
            legacy = Path(root) / "resume_profile.json"
            resume.write_text("Python", encoding="utf-8")
            profile = factual_profile()
            profile["_resume_path"] = str(resume.resolve())
            profile["_resume_sha256"] = resume_digest(resume)
            legacy.write_text(json.dumps(profile), encoding="utf-8")
            fake = Mock()

            with patch("resume_profile.PROFILE_PATH", str(legacy)):
                loaded = get_or_build_profile(resume, gemini=fake, cache_dir=cache)

            self.assertEqual(loaded["_resume_sha256"], resume_digest(resume))
            fake.generate_json_object.assert_not_called()
            self.assertTrue(profile_cache_path(resume, cache).exists())

    def test_stale_legacy_profile_is_not_imported(self):
        with tempfile.TemporaryDirectory() as root:
            resume = Path(root) / "backend.txt"
            cache = Path(root) / "cache"
            legacy = Path(root) / "resume_profile.json"
            resume.write_text("Python and Django", encoding="utf-8")
            profile = factual_profile()
            profile["_resume_path"] = str(resume.resolve())
            profile["_resume_sha256"] = "stale"
            legacy.write_text(json.dumps(profile), encoding="utf-8")
            fake = Mock()
            fake.generate_json_object.return_value = factual_profile()

            with patch("resume_profile.PROFILE_PATH", str(legacy)):
                loaded = get_or_build_profile(resume, gemini=fake, cache_dir=cache)

            self.assertEqual(loaded["_resume_sha256"], resume_digest(resume))
            fake.generate_json_object.assert_called_once()

    def test_invalid_profile_shape_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "skills"):
            validate_resume_profile({"skills": "Python"})

    def test_invalid_profile_list_contents_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "roles"):
            validate_resume_profile({"roles": [{}]})

    def test_cache_write_leaves_no_temporary_file(self):
        with tempfile.TemporaryDirectory() as root:
            resume = Path(root) / "backend.txt"
            cache = Path(root) / "cache"
            resume.write_text("Python", encoding="utf-8")
            fake = Mock()
            fake.generate_json_object.return_value = factual_profile()

            get_or_build_profile(resume, gemini=fake, cache_dir=cache)

            self.assertEqual(list(cache.glob("*.tmp")), [])


class ResumeRankingTests(unittest.TestCase):
    def test_backend_profile_beats_frontend_profile_for_python_api_job(self):
        job = {
            "title": "Python Backend Engineer",
            "description": "Build REST APIs for backend services",
            "required_skills": ["Python", "Django", "REST APIs"],
            "preferred_skills": ["Docker"],
            "responsibilities": ["Build backend APIs"],
            "matched_roles": ["Backend Engineer"],
        }
        backend = factual_profile()
        backend["skills"] += ["REST API", "Docker"]
        frontend = factual_profile("Frontend Engineer")
        frontend.update(
            {
                "skills": ["React", "JavaScript"],
                "skill_years": {"React": 2},
                "roles": ["Frontend Engineer"],
                "domains": ["UI"],
                "projects": [],
                "keywords": ["frontend"],
                "target_role_categories": ["frontend"],
            }
        )

        ranked = rank_resumes(job, {"backend.pdf": backend, "frontend.pdf": frontend})

        self.assertEqual(ranked[0]["path"], "backend.pdf")
        self.assertGreater(ranked[0]["score"], ranked[1]["score"])
        self.assertIn("Python", ranked[0]["strong_matches"])
        self.assertEqual(ranked[0]["missing_required"], [])

    def test_skill_aliases_are_canonical(self):
        self.assertEqual(canonical_skill("REST API"), "rest api")
        self.assertEqual(canonical_skill("REST APIs"), "rest api")
        self.assertEqual(canonical_skill("Postgres"), "postgresql")

    def test_ranking_reports_partial_and_missing_skills(self):
        job = {
            "title": "AI Backend Engineer",
            "description": "AI integration services",
            "required_skills": ["AI integration", "Python"],
            "preferred_skills": ["Kubernetes"],
            "responsibilities": [],
            "matched_roles": ["AI Backend Engineer"],
        }
        profile = factual_profile()
        profile["skills"].append("LangChain")

        result = rank_resumes(job, {"backend.pdf": profile})[0]

        self.assertIn("Python", result["strong_matches"])
        self.assertIn("AI integration", result["partial_matches"])
        self.assertEqual(result["missing_preferred"], ["Kubernetes"])
        self.assertIn("Kubernetes", result["reasoning"])

    def test_equal_scores_use_stable_path_order(self):
        job = {
            "title": "Backend Engineer",
            "required_skills": ["Python"],
            "preferred_skills": [],
            "responsibilities": [],
            "matched_roles": ["Backend Engineer"],
        }
        profile = factual_profile()

        ranked = rank_resumes(job, {"z.pdf": profile, "a.pdf": profile})

        self.assertEqual([item["path"] for item in ranked], ["a.pdf", "z.pdf"])

    def test_choose_resume_path_keeps_existing_two_argument_call(self):
        with tempfile.TemporaryDirectory() as root:
            fallback = Path(root) / "resume.pdf"
            fallback.write_bytes(b"resume")
            with patch("linkedin_search.load_resume_profiles", return_value={}):
                selected = choose_resume_path(
                    {"title": "Unknown role", "description": ""},
                    {"resume_path": str(fallback)},
                )

        self.assertEqual(selected, str(fallback))


if __name__ == "__main__":
    unittest.main()
