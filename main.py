import json
import hashlib
import secrets
from pathlib import Path

from nicegui import ui, app
from dotenv import load_dotenv
import os
import requests
from search import search
# Load environment variables from .env file
load_dotenv()
project_dir = Path(__file__).resolve().parent

@ui.page('/auth/github/callback')
def github_callback_page(code: str = '', state: str = ''):
    if not code or not state:
        ui.label('Missing code or state parameter.')
        return
    if state != app.storage.user.get('state'):
        ui.label('Invalid state parameter. Possible CSRF attack.')
        return
    del app.storage.user['state']  # Remove the state parameter from storage after validation
    url = "https://github.com/login/oauth/access_token"
    request_para = {
        "client_id": os.getenv("GITHUB_CLIENT_ID"),
        "client_secret": os.getenv("GITHUB_CLIENT_SECRET"),
        "code": code,
        "redirect_uri": os.getenv("GITHUB_REDIRECT_URI"),
    }
    # Keep the request itself inside try: connection errors happen before a response exists.
    try:
        response = requests.post(url, data=request_para, headers={"Accept": "application/json"}, timeout=10)
        response.raise_for_status()
        result = response.json()
    except requests.exceptions.JSONDecodeError:
        ui.label("GitHub returned invalid token data. Please sign in again.")
        return
    except requests.exceptions.Timeout:
        ui.label("The token request timed out. Please sign in again.")
        return
    except requests.exceptions.HTTPError:
        ui.label("GitHub rejected the token request. Please sign in again.")
        return
    except requests.exceptions.RequestException:
        ui.label("Could not connect to GitHub to obtain a token. Please sign in again.")
        return
    if not isinstance(result, dict):
        ui.label("GitHub returned an unexpected token response. Please sign in again.")
        return
    if 'error' in result:
        ui.label("error: " + result['error'])
        return
    if result.get('access_token'):
        access_token = result['access_token']
        try:
            user_response = requests.get("https://api.github.com/user", headers={"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2026-03-10", "Authorization": f"Bearer {access_token}"}, timeout=10)
            user_response.raise_for_status()
            user_info = user_response.json()
        except requests.exceptions.JSONDecodeError:
            ui.label("GitHub returned invalid user data. Please sign in again.")
            return
        except requests.exceptions.Timeout:
            ui.label("The user information request timed out. Please sign in again.")
            return
        except requests.exceptions.HTTPError:
            ui.label("GitHub rejected the user information request. Please sign in again.")
            return
        except requests.exceptions.RequestException:
            ui.label("Could not connect to GitHub to fetch user information. Please sign in again.")
            return
        if not isinstance(user_info, dict) or not user_info.get('login'):
            ui.label("GitHub returned user information without a username. Please sign in again.")
            return
        ui.label(f"Hello, {user_info.get('login')}! You have successfully signed in with GitHub.")
        USERNAME = user_info.get('login')
    else:
        ui.label("No access token received.")
        return

    try:
        page = 1
        all_starred_repos = []
        while True:
            starred_response = requests.get(f"https://api.github.com/users/{USERNAME}/starred", headers={"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2026-03-10", "Authorization": f"Bearer {access_token}"}, timeout=10, params={"per_page": 100, "page": page})
            starred_response.raise_for_status()
            starred_repos = starred_response.json()
            
            all_starred_repos.extend(starred_repos)
            page += 1
            if len(starred_repos) < 100:
                break
        ui.label(f"You have starred {len(all_starred_repos)} repositories on GitHub.")
        for repo in all_starred_repos:
            ui.label(f"Repository: {repo.get('full_name')} - Description: {repo.get('description')}")

        with open(project_dir / "starred_repos.json", "w", encoding="utf-8") as f:
            json.dump(all_starred_repos, f)

        
    except requests.exceptions.JSONDecodeError:
        ui.label("GitHub returned invalid starred repositories data. Please try again.")
        return
    except requests.exceptions.Timeout:
        ui.label("The starred repositories request timed out. Please try again.")
        return
    except requests.exceptions.HTTPError:
        ui.label("GitHub rejected the starred repositories request. Please try again.")
        return
    except requests.exceptions.RequestException:
        ui.label("Could not connect to GitHub to fetch starred repositories. Please try again.")
        return
    except OSError:
        ui.label("Could not save starred repositories to disk.")
        return

    ui.label("Run analyse.py after syncing your stars to generate search keywords.")
    ui.link('Search repositories', '/search')

@ui.page('/auth/github')
def sign_github_page():
    app.storage.user['state'] = secrets.token_urlsafe(16) # Generate a secure random state parameter
    ui.button('Sign in with GitHub', on_click=lambda: ui.navigate.to(f'https://github.com/login/oauth/authorize?client_id={os.getenv("GITHUB_CLIENT_ID")}&redirect_uri={os.getenv("GITHUB_REDIRECT_URI")}&state={app.storage.user["state"]}'))


@ui.page('/')
def main_page():
    ui.label('What are you looking for?')
    ui.button('github', on_click=lambda: ui.navigate.to('/auth/github'))
    ui.link('Search with saved keywords', '/search')

@ui.page('/search')
def search_page():
    ui.label('Repository search')
    # This first version reads the single local user's saved analysis.
    try:
        keyword_data = json.loads((project_dir / 'search_keywords.json').read_text(encoding='utf-8'))
        source_content = (project_dir / 'starred_repos.json').read_bytes()
    except FileNotFoundError:
        ui.label('Sync your stars and run analyse.py first, then open this page again.')
        return
    except (OSError, UnicodeError, json.JSONDecodeError):
        ui.label('Could not read the saved search data. Please run analyse.py again.')
        return

    if not isinstance(keyword_data, dict) or not isinstance(keyword_data.get('keywords'), list):
        ui.label('Invalid keyword file. Please run analyse.py again.')
        return
    if keyword_data.get('source_sha256') != hashlib.sha256(source_content).hexdigest():
        ui.label('Your saved stars have changed. Run analyse.py again before searching.')
        return
    keywords = keyword_data['keywords']
    if not all(isinstance(keyword, str) and keyword.strip() for keyword in keywords):
        ui.label('Invalid keywords. Please run analyse.py again.')
        return
    if not keywords:
        ui.label('No search keywords were generated.')
        return

    selected_keywords = list(dict.fromkeys(keywords))
    ui.label(f'Searching {len(selected_keywords)} unique keywords, with up to 10 results each.')
    ui.label('Keywords: ' + ', '.join(selected_keywords))
    # Index by repository ID to merge duplicates and keep their matching keywords.
    repos_by_id = {}
    successful_searches = 0
    partial_results = False
    for keyword in selected_keywords:
        result = search(keyword)
        if result is None:
            ui.label(f'Search failed for: {keyword}. Stopped further requests.')
            partial_results = True
            break
        if not isinstance(result, dict) or not isinstance(result.get('items'), list):
            ui.label(f'Unexpected search response for: {keyword}.')
            partial_results = True
            break
        items = result['items']
        if not all(isinstance(repo, dict) and isinstance(repo.get('id'), int) for repo in items):
            ui.label(f'Invalid repository data for: {keyword}.')
            partial_results = True
            break
        successful_searches += 1
        if result.get('incomplete_results'):
            ui.label(f'GitHub reported incomplete results for: {keyword}.')
            partial_results = True
        for repo in items:
            if repo['id'] not in repos_by_id:
                repos_by_id[repo['id']] = {**repo, 'matched_keywords': []}
            repos_by_id[repo['id']]['matched_keywords'].append(keyword)

    all_repos = list(repos_by_id.values())
    ui.label(f'{successful_searches} searches succeeded; {len(all_repos)} unique candidates collected.')
    if partial_results:
        ui.label('Some searches failed or were incomplete; these are partial results.')
    elif not all_repos:
        ui.label('No repositories matched these keywords.')
    for repo in all_repos:
        ui.label(repo.get('full_name', 'Unnamed repository'))
        star_count = repo.get('stargazers_count')
        if isinstance(star_count, int) and star_count >= 0:
            ui.label(f'★ {star_count:,} stars')
        else:
            ui.label('★ Star count unavailable')
        ui.label(repo.get('description') or 'No description available.')
        repo_url = repo.get('html_url')
        if isinstance(repo_url, str) and repo_url.startswith('https://github.com/'):
            ui.link('View on GitHub', repo_url)
        ui.label('Matched keywords: ' + ', '.join(repo['matched_keywords']))

ui.run(host='127.0.0.1', port=8080, storage_secret=os.getenv("STORAGE_SECRET"))
