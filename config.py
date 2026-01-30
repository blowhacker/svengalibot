import os
from pathlib import Path

BASE_DIR = Path(__file__).parent.absolute()
DATA_DIR = BASE_DIR / "data"


class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-change-in-prod")

    # Paths
    DATA_DIR = DATA_DIR
    TASKS_DIR = DATA_DIR / "tasks"
    REPOS_DIR = DATA_DIR / "repos"
    GUIDE_PATH = DATA_DIR / "guide.yaml"
    CONFIG_PATH = DATA_DIR / "config.yaml"

    # OpenAI
    OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
    OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "o1-preview")

    # Flask
    DEBUG = os.environ.get("FLASK_DEBUG", "0") == "1"
    HOST = os.environ.get("FLASK_HOST", "0.0.0.0")
    PORT = int(os.environ.get("FLASK_PORT", "5000"))


class DevelopmentConfig(Config):
    DEBUG = True


class ProductionConfig(Config):
    DEBUG = False


config = {
    "development": DevelopmentConfig,
    "production": ProductionConfig,
    "default": DevelopmentConfig,
}
