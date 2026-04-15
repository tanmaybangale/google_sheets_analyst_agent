"""
=============================================================================
File: config.py
Capabilities:
1. Environment Management: Explicitly loads and validates core GCP 
   configuration from the .env file. Fails fast if critical variables are missing.
2. Centralized Logging: Provides a uniform, configurable logging setup for the whole app.
=============================================================================
"""

import logging
import os
from dotenv import load_dotenv

# 1. Load the environment variables from .env
load_dotenv()

# Get the absolute path to the directory containing config.py
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 2. Explicitly fetch and store the variables
PROJECT_ID = os.getenv("GOOGLE_CLOUD_PROJECT")
LOCATION = os.getenv("GOOGLE_CLOUD_LOCATION")
USE_VERTEXAI = os.getenv("GOOGLE_GENAI_USE_VERTEXAI", "False").lower() == "true"

# Dynamic Log Level for debugging the complex multi-file engine (Default: INFO)
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

# --- Authentication Setup ---
# FOR DEPLOYMENT/PRODUCTION: We rely on Google's Application Default Credentials (ADC).
# FOR LOCAL RUNS: The Google Cloud SDK automatically reads the GOOGLE_APPLICATION_CREDENTIALS
# variable from your .env file. We no longer hardcode or strictly require the file path here.

# --- OAuth Configuration ---
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")

# Define logger BEFORE validation so we can safely use it
def get_logger(name: str) -> logging.Logger:
    """
    Configures and returns a structured logger.
    
    Args:
        name (str): The name of the module requesting the logger (e.g., __name__).
        
    Returns:
        logging.Logger: A configured logger instance.
    """
    logger = logging.getLogger(name)
    
    # Prevent adding multiple handlers if get_logger is called multiple times
    if not logger.handlers:
        handler = logging.StreamHandler()
        formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        handler.setFormatter(formatter)
        logger.addHandler(handler)
        
        # Map the string LOG_LEVEL to the logging module's integer constants
        level = getattr(logging, LOG_LEVEL, logging.INFO)
        logger.setLevel(level)
        handler.setLevel(level)
        
    return logger

# Initialize a logger for the config module itself
init_logger = get_logger(__name__)
init_logger.debug("Initializing configuration and validating environment variables...")

# 3. Fail-Fast Validation
if not PROJECT_ID:
    init_logger.error("GOOGLE_CLOUD_PROJECT is missing from the environment.")
    raise ValueError("CRITICAL: GOOGLE_CLOUD_PROJECT is missing from the .env file.")
    
if not LOCATION:
    init_logger.error("GOOGLE_CLOUD_LOCATION is missing from the environment.")
    raise ValueError("CRITICAL: GOOGLE_CLOUD_LOCATION is missing from the .env file.")
    
if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
    init_logger.warning("OAuth Client ID or Secret is missing. End-user Google Drive login will fail.")

# Log the initialization configuration safely
init_logger.info(f"Configuration loaded successfully. Project: {PROJECT_ID}, Location: {LOCATION}, Log Level: {LOG_LEVEL}")