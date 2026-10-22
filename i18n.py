"""Minimal bilingual UI helper.

Language is stored per browser session. Pages are re-rendered after switching,
so labels can be translated at render time without a reactive layer.
"""

from nicegui import app

LANGUAGE_KEY = 'language'
SUPPORTED_LANGUAGES = ('zh', 'en')


def current_language() -> str:
    try:
        language = app.storage.user.get(LANGUAGE_KEY)
    except Exception:
        # Tests and import-time calls do not have a browser session.
        return 'zh'
    return language if language in SUPPORTED_LANGUAGES else 'zh'


def set_language(language: str) -> None:
    if language in SUPPORTED_LANGUAGES:
        app.storage.user[LANGUAGE_KEY] = language


def L(chinese: str, english: str) -> str:
    """Return English in English mode, Chinese otherwise."""
    return english if current_language() == 'en' else chinese
