import json

from nicegui import ui, app
from dotenv import load_dotenv
import requests

def search(keywords: str = ''):
    try:
        reponse = requests.get("https://api.github.com/search/repositories", headers={"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2026-03-10"}, timeout=10, params={"q": keywords, "per_page": 10, "page": 1, "sort": "stars", "order": "desc"})
        reponse.raise_for_status()
        return reponse.json()
    except requests.exceptions.JSONDecodeError:
        ui.label("GitHub returned invalid search data. Please try again.")
        return
    except requests.exceptions.Timeout:
        ui.label("The search request timed out. Please try again.")
        return
    except requests.exceptions.HTTPError:
        ui.label("GitHub rejected the search request. Please try again.")
        return
    except requests.exceptions.RequestException:
        ui.label("Could not connect to GitHub to perform the search. Please try again.")
        return
