"""
=============================================================================
File: tools.py
Capabilities:
1. ADK Tool Wrappers: Flat, easily parsed functions for Google ADK schema generation.
2. Delegation: Passes context down to `auth.py` and `engine.py` for execution.
=============================================================================
"""

import os
import re
from google.adk.tools import ToolContext

# Import our modular backend components (Dots removed for Root Deployment)
from .config import get_logger
from .engine import SheetDataEngine
from .auth import get_drive_service

logger = get_logger(__name__)

# --- GLOBAL ENGINE INSTANCE ---
# Singleton pattern to prevent re-initialization conflicts in serverless
if 'data_engine' not in globals():
    data_engine = SheetDataEngine()

# ===========================================================================
# ADK TOOL WRAPPERS
# ===========================================================================

def download_drive_file(file_identifiers: str, tool_context: ToolContext) -> str:
    """
    Downloads one or multiple comma-separated Google Drive File IDs.
    Extracts schemas and creates temporary CSVs for DuckDB querying.
    """
    logger.info(f"--- Tool Called: download_drive_file for {file_identifiers} ---")
    
    drive_service = get_drive_service(tool_context)
    if not drive_service: 
        return "Error: Authentication missing. Please refresh the chat to log in again."
        
    return data_engine.download_and_extract(file_identifiers, drive_service)

def sheet_nl2sql(natural_language_question: str, schema_info: str) -> str:
    """
    Translates a natural language question into standard DuckDB SQL 
    based on the provided mega-schema.
    """
    logger.info("--- Tool Called: sheet_nl2sql ---")
    return f"""
    SYSTEM INSTRUCTION TO AGENT: 
    Task: Write a DuckDB SQL query for: "{natural_language_question}"
    Rules:
    1. Use exact tables and columns from: {schema_info}
    2. Use ILIKE with wildcards (e.g., %word1%word2%) for text filtering.
    3. Immediately call `execute_sql_on_file` with your SQL.
    """

def execute_sql_on_file(sql_query: str, file_paths: str) -> str:
    """
    Executes DuckDB SQL across the downloaded files.
    """
    logger.info("--- Tool Called: execute_sql_on_file ---")
    summary, df = data_engine.execute_sql(sql_query)
    
    if df is not None:
        import tempfile
        temp_file = os.path.join(tempfile.gettempdir(), "last_query_result.csv")
        df.to_csv(temp_file, index=False)
        logger.info(f"Saved query result to {temp_file} for artifact processing.")
        summary += f"\n\n[System Note: Data saved for artifact processing. Use `get_sql_report_csv` to load it for charting.]"
        
    return summary

def list_drive_folder(folder_url: str, tool_context: ToolContext) -> str:
    """
    Scans a Google Drive folder and lists available files and their IDs.
    """
    logger.info(f"--- Tool Called: list_drive_folder for {folder_url} ---")
    
    drive_service = get_drive_service(tool_context)
    if not drive_service: 
        return "Error: Authentication missing. Please refresh the chat to log in again."
        
    match = re.search(r"/(?:d|folders)/([a-zA-Z0-9_-]+)", folder_url)
    folder_id = match.group(1) if match else folder_url
    
    try:
        results = drive_service.files().list(
            q=f"'{folder_id}' in parents and trashed = false", 
            fields="files(id, name, mimeType)", 
            pageSize=50
        ).execute()
        
        files = results.get('files', [])
        if not files:
            return "The folder is empty or the agent does not have permission to view it."

        output = f"Found {len(files)} files:\n"
        for f in files:
            file_type = 'Folder' if f['mimeType'] == 'application/vnd.google-apps.folder' else 'File'
            output += f"- [{file_type}] {f['name']} (ID: {f['id']})\n"
            
        return output
    except Exception as e:
        logger.error(f"Folder scan failed: {e}")
        return f"Scan failed: {e}"

async def get_sql_report_csv(tool_context: ToolContext) -> str:
    """
    Loads the last SQL query result artifact and returns its content as a string.
    Use this to get the data for charting if the dataset is large.
    """
    logger.info("--- Tool Called: get_sql_report_csv ---")
    try:
        csv_artifact = await tool_context.load_artifact(filename="last_report.csv")
        if csv_artifact and csv_artifact.inline_data:
            csv_content = csv_artifact.inline_data.data.decode('utf-8')
            return csv_content
        else:
            return "Error: Artifact 'last_report.csv' not found. Please ensure it was generated."
    except Exception as e:
        return f"An unexpected error occurred while loading artifact: {e}"
