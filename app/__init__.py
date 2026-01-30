from flask import Flask
from config import config
import os


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

    # Register blueprints
    from app.routes import main_bp
    app.register_blueprint(main_bp)

    return app
