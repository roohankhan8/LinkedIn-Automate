import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

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
            profile["_resume_text"] = "Python"
            legacy.write_text(json.dumps(profile), encoding="utf-8")
            fake = Mock()

            with patch("resume_profile.PROFILE_PATH", str(legacy)):
                loaded = get_or_build_profile(resume, gemini=fake, cache_dir=cache)

            self.assertEqual(loaded["_resume_sha256"], resume_digest(resume))
            fake.generate_json_object.assert_not_called()
            self.assertTrue(profile_cache_path(resume, cache).exists())

    def test_invalid_profile_shape_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "skills"):
            validate_resume_profile({"skills": "Python"})

    def test_cache_write_leaves_no_temporary_file(self):
        with tempfile.TemporaryDirectory() as root:
            resume = Path(root) / "backend.txt"
            cache = Path(root) / "cache"
            resume.write_text("Python", encoding="utf-8")
            fake = Mock()
            fake.generate_json_object.return_value = factual_profile()

            get_or_build_profile(resume, gemini=fake, cache_dir=cache)

            self.assertEqual(list(cache.glob("*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
