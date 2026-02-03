#!/usr/bin/env python3
import logging
from dotenv import load_dotenv
load_dotenv()

# Configure logging to both console and file
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s [%(name)s] %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('svengalibot.log'),
    ]
)

from app import create_app

app = create_app()

if __name__ == "__main__":
    logging.info("Starting Svengalibot...")
    app.run(
        host=app.config.get("HOST", "0.0.0.0"),
        port=app.config.get("PORT", 5011),
        debug=app.config.get("DEBUG", True),
    )
