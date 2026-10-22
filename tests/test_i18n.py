"""Bilingual helper tests; no browser session required."""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

import i18n


class I18nTests(unittest.TestCase):
    def test_english_mode_returns_english(self):
        with patch.object(i18n, 'app', SimpleNamespace(
            storage=SimpleNamespace(user={'language': 'en'})
        )):
            self.assertEqual(i18n.L('中文', 'English'), 'English')

    def test_default_and_unknown_languages_fall_back_to_chinese(self):
        for session in ({}, {'language': 'fr'}, {'language': 'zh'}):
            with patch.object(i18n, 'app', SimpleNamespace(
                storage=SimpleNamespace(user=session)
            )):
                self.assertEqual(i18n.L('中文', 'English'), '中文')


if __name__ == '__main__':
    unittest.main()
