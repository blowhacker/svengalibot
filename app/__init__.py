from flask import Flask
from config import config
import os
import yaml


def _load_api_keys(data_dir):
    """Load API keys from keys.yaml and set as environment variables."""
    keys_path = data_dir / "keys.yaml"
    if keys_path.exists():
        try:
            with open(keys_path) as f:
                keys = yaml.safe_load(f) or {}
            if keys.get("openai_api_key"):
                os.environ.setdefault("OPENAI_API_KEY", keys["openai_api_key"])
            if keys.get("anthropic_api_key"):
                os.environ.setdefault("ANTHROPIC_API_KEY", keys["anthropic_api_key"])
            if keys.get("google_api_key"):
                os.environ.setdefault("GOOGLE_API_KEY", keys["google_api_key"])
        except Exception:
            pass  # Keys file might be malformed, ignore


def create_app(config_name=None):
    if config_name is None:
        config_name = os.environ.get("FLASK_ENV", "default")

    app = Flask(__name__,
                template_folder="../templates",
                static_folder="../static")

    app.config.from_object(config[config_name])

    # Ensure data directories exist
    app.config["TASKS_DIR"].mkdir(parents=True, exist_ok=True)
    app.config["REPOS_DIR"].mkdir(parents=True, exist_ok=True)

    # Load API keys from keys.yaml
    _load_api_keys(app.config["DATA_DIR"])

    # Register blueprints
    from app.routes import main_bp
    app.register_blueprint(main_bp)

    return app
