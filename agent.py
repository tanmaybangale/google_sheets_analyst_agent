"""
=============================================================================
File: agent.py
Capabilities:
1. Agent Definition: Defines the core 'SheetDataScienceAgent'.
2. Tool Binding: Connects the agent directly to our modular ADK tool wrappers.
=============================================================================
"""

import logging
import os
from google.adk.agents import Agent
from google.adk.tools import AgentTool
from google.adk.code_executors import BuiltInCodeExecutor

# Import our ADK tool wrappers, including the restored SQL bridge tool
from .tools import (
    list_drive_folder, 
    download_drive_file, 
    sheet_nl2sql,         # <-- The Natural Language to SQL bridge
    execute_sql_on_file, 
    get_sql_report_csv  # <-- ADDED
)
from google.adk.agents.callback_context import CallbackContext
from google.genai import types
import io
import tempfile
from .prompts import return_instructions_sheet

try:
    from .config import get_logger
    logger = get_logger(__name__)
except ImportError:
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
    logger = logging.getLogger(__name__)

async def save_last_query_result_as_artifact(callback_context: CallbackContext, **kwargs):
    """
    Reads /tmp/last_query_result.csv and saves it as an artifact named 'last_report.csv'.
    """
    import os
    temp_file = os.path.join(tempfile.gettempdir(), "last_query_result.csv")
    if os.path.exists(temp_file):
        try:
            logger.info("Found query result file. Saving as artifact...")
            with open(temp_file, 'rb') as f:
                csv_bytes = f.read()
            
            csv_artifact = types.Part(
                inline_data=types.Blob(mime_type="text/csv", data=csv_bytes)
            )
            
            version = await callback_context.save_artifact(
                filename="last_report.csv",
                artifact=csv_artifact
            )
            logger.info(f"Artifact 'last_report.csv' saved as version {version}.")
            
            # Delete the temp file
            os.remove(temp_file)
            logger.info("Deleted temp file.")
        except Exception as e:
            logger.error(f"Failed to save artifact: {e}")

def code_executor_agent():
    instruction = (
        "You are a Python Data Science and Visualization Expert. Your goal is to transform "
        "raw data into actionable visual insights.\n\n"
        "OPERATIONAL GUIDELINES:\n"
        "1. DATA PROCESSING: Use pandas and numpy for all data manipulation. Always "
        "check for and handle missing values or incorrect data types (e.g., convert strings to dates/floats).\n"
        "2. VISUALIZATION STANDARDS: Use matplotlib and seaborn for plotting.\n"
        "   - Every chart MUST have: A descriptive Title, X/Y Axis Labels, and a Legend if multiple series exist.\n"
        "   - Styling: Use sns.set_theme(style='whitegrid') for a clean, professional look.\n"
        "   - Readability: Rotate axis labels if they overlap and use distinct color palettes.\n"
        "3. FORMATTING: Use thousand separators (e.g., 1,250,000) for data labels and axis ticks "
        "to ensure consistency with the main agent's output.\n"
        "4. ARTIFACT GENERATION: You MUST save your final plot as a PNG file to artifacts (e.g., 'sales_analysis_timestamp.png'). "
        "This file will be automatically processed as an artifact for the user.\n"
        "5. ERROR HANDLING: If the input dataset is empty or invalid, do not attempt to plot; "
        "instead, return a clear message explaining why the visualization could not be created.\n"
        "6. STORAGE CLEANUP: If you read data from a temporary file, you MUST delete the file using `os.remove()` after loading it into memory to prevent storage overload."
    )
 
    return Agent(
        name='code_exec_agent',
        model="gemini-2.5-flash",
        description="Data Visualization and Analysis Expert. Delegate to this agent for generating charts, plots, and complex Python calculations.",
        code_executor=BuiltInCodeExecutor(),
        instruction=instruction
    )

code_exec_tool = AgentTool(code_executor_agent())

# Add tools to the Agent directly
root_agent = Agent(
    name="SheetDataScienceAgent",
    model="gemini-2.5-flash", 
    description="An enterprise agent that analyzes and transforms Google Drive spreadsheet data (Excel/CSV) using DuckDB SQL.",
    instruction=return_instructions_sheet(),
    tools=[
        list_drive_folder,
        download_drive_file, 
        sheet_nl2sql,         
        execute_sql_on_file, 
        get_sql_report_csv,  # <-- ADDED
        code_exec_tool
    ],
    before_model_callback=save_last_query_result_as_artifact # <-- ADDED
)

logger.info("SheetDataScienceAgent initialized and tools bound successfully.")