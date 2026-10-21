import secrets

from nicegui import ui, app
from dotenv import load_dotenv
import os

# Load environment variables from .env file
load_dotenv()

@ui.page('/auth/github/callback')
def github_callback_page(code: str = '', state: str = ''):
    if not code or not state:
        ui.label('Missing code or state parameter.')
        return
    if state != app.storage.user.get('state'):
        ui.label('Invalid state parameter. Possible CSRF attack.')
        return
    del app.storage.user['state']  # Remove the state parameter from storage after validation
    ui.label("callback validated")
@ui.page('/auth/github')
def sign_github_page():
    app.storage.user['state'] = secrets.token_urlsafe(16) # Generate a secure random state parameter
    ui.button('Sign in with GitHub', on_click=lambda: ui.navigate.to(f'https://github.com/login/oauth/authorize?client_id={os.getenv("GITHUB_CLIENT_ID")}&redirect_uri={os.getenv("GITHUB_REDIRECT_URI")}&state={app.storage.user["state"]}'))


@ui.page('/')
def main_page():
    ui.label('What are you looking for?')
    ui.button('github', on_click=lambda: ui.navigate.to('/auth/github'))


ui.run(host='127.0.0.1', port=8080, storage_secret=os.getenv("STORAGE_SECRET"))