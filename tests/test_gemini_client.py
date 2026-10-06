import unittest
from unittest.mock import Mock

from gemini_client import Gemini, GeminiError, _parse_json


class GeminiStructuredResponseTests(unittest.TestCase):
    def test_generate_json_object_rejects_array(self):
        client = Gemini.__new__(Gemini)
        client.generate_json = Mock(return_value=[{"answer": "Python"}])

        with self.assertRaisesRegex(GeminiError, "JSON object"):
            client.generate_json_object("prompt")

    def test_malformed_json_raises_gemini_error(self):
        with self.assertRaisesRegex(GeminiError, "did not return JSON"):
            _parse_json("not JSON at all")


if __name__ == "__main__":
    unittest.main()
