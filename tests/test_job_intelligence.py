import unittest

from job_intelligence import enabled_role_profiles, normalize_config, recency_filter


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


if __name__ == "__main__":
    unittest.main()
