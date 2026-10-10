import unittest

from playwright.sync_api import sync_playwright

from easy_apply import find_modal


class EasyApplyModalTests(unittest.TestCase):
    def test_finds_dialog_when_linkedin_uses_non_div_element(self):
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page()
            page.set_content('<section role="dialog">Apply to Example Corp</section>')

            modal = find_modal(page)

            self.assertIsNotNone(modal)
            self.assertEqual(modal.text_content(), "Apply to Example Corp")
            browser.close()


if __name__ == "__main__":
    unittest.main()
