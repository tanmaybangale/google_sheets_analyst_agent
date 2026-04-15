"""
=============================================================================
File: engine.py
Capabilities:
1. Object-Oriented State: Tracks file formats (CSV vs XLSX) across AI turns.
2. Background CSV Extraction: Converts Excel tabs to CSVs for DuckDB disk speed.
3. Multi-File JOINs: Registers multiple files into a single DuckDB context.
4. SQL Sanitization: Handles hyphens and special characters in File IDs.
5. Dynamic GCS Signing: Generates secure V4 download links via Metadata identity.
=============================================================================
"""

import os
import tempfile
import duckdb
import pandas as pd
import uuid
import re
import datetime
import requests # Standard requests for Metadata
from typing import Dict, List, Tuple, Optional
from googleapiclient.http import MediaIoBaseDownload

# Absolute import for root-level deployment (Important for Cloud Run/ADK)
from .config import get_logger

logger = get_logger(__name__)

class SheetDataEngine:
    def __init__(self):
        # In-memory connection avoids "Conflicting Lock" errors in serverless environments
        self.con = duckdb.connect(':memory:') 
        self.original_formats = {}  # Format: { 'file_id': '.xlsx' }
        self.registered_tables = []
        logger.info("SheetDataEngine initialized in-memory.")

    def _process_df_headers(self, df: pd.DataFrame) -> pd.DataFrame:
        """Heuristic to find or generate headers for a messy dataframe."""
        if all(isinstance(col, int) for col in df.columns):
            header_row_idx = -1
            for i in range(min(10, len(df))):
                row = df.iloc[i]
                non_null_values = row[row.notna()]
                if len(non_null_values) == 0: continue
                            
                is_all_str = all(isinstance(v, str) for v in non_null_values)
                if is_all_str and len(non_null_values) >= len(df.columns) / 2:
                    header_row_idx = i
                    break
                                
                is_any_num = any(isinstance(v, (int, float)) for v in non_null_values)
                if is_any_num:
                    if i == 0: break
                    break
                    
            if header_row_idx != -1:
                headers = df.iloc[header_row_idx].tolist()
                cleaned_headers = [
                    f"Column_{idx}" if str(h).lower() == "nan" or str(h).strip() == "" else str(h)
                    for idx, h in enumerate(headers)
                ]
                df.columns = cleaned_headers
                df = df.iloc[header_row_idx + 1:].reset_index(drop=True)
            else:
                df.columns = [f"col_{i}" for i in range(len(df.columns))]
                
        return df
    
    def _detect_and_split_tables(self, df: pd.DataFrame) -> List[pd.DataFrame]:
        """Detects and splits multiple tables in a dataframe separated by empty rows/cols."""
        null_rows = df.isna().all(axis=1)
        null_cols = df.isna().all(axis=0)
        
        if not null_rows.any() and not null_cols.any():
            return [df]
            
        row_blocks = []
        current_block = []
        for idx, is_null in enumerate(null_rows):
            if not is_null:
                current_block.append(idx)
            elif current_block:
                row_blocks.append(current_block)
                current_block = []
        if current_block:
            row_blocks.append(current_block)
            
        tables = []
        for block in row_blocks:
            sub_df = df.iloc[block]
            
            sub_null_cols = sub_df.isna().all(axis=0)
            col_blocks = []
            current_col_block = []
            for c_idx, c_is_null in enumerate(sub_null_cols):
                if not c_is_null:
                    current_col_block.append(c_idx)
                elif current_col_block:
                    col_blocks.append(current_col_block)
                    current_col_block = []
            if current_col_block:
                col_blocks.append(current_col_block)
                
            for c_block in col_blocks:
                final_df = sub_df.iloc[:, c_block]
                if not final_df.empty:
                    final_df = final_df.reset_index(drop=True)
                    final_df.columns = range(final_df.shape[1])
                    tables.append(final_df)
                    
        return tables

    def download_and_extract(self, file_identifiers: str, drive_service) -> str:
        """Downloads files, sanitizes names for SQL, and builds a mega-schema."""
        ids = [i.strip() for i in file_identifiers.split(',')]
        temp_dir = tempfile.gettempdir()
        mega_schema = "MEGA-SCHEMA FOR REQUESTED FILES:\n\n"
        successful_paths = []

        for url_or_id in ids:
            match = re.search(r"/(?:d|folders)/([a-zA-Z0-9_-]+)", url_or_id)
            file_id = match.group(1) if match else url_or_id
            
            # SANITIZATION: Replace hyphens and special chars with underscores for SQL compliance
            # This prevents "Parser Error: syntax error at or near '-'"
            safe_file_id = re.sub(r'[^a-zA-Z0-9]', '_', file_id)
            
            try:
                meta = drive_service.files().get(fileId=file_id, fields='name, mimeType', supportsAllDrives=True).execute()
                mime = meta.get('mimeType')
                original_name = meta.get('name', 'Unknown')
                
                ext = ".xlsx" if "spreadsheet" in mime or "excel" in mime else ".csv"
                self.original_formats[file_id] = ext
                
                raw_path = os.path.join(temp_dir, f"raw_{file_id}{ext}")
                request = drive_service.files().export_media(fileId=file_id, mimeType='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet') if 'google-apps.spreadsheet' in mime else drive_service.files().get_media(fileId=file_id, supportsAllDrives=True)
                
                logger.info(f"Downloading {original_name}...")
                with open(raw_path, 'wb') as fh:
                    downloader = MediaIoBaseDownload(fh, request)
                    done = False
                    while not done:
                        _, done = downloader.next_chunk()

                mega_schema += f"--- Source File: {original_name} (ID: {file_id}) ---\n"
                
                if ext == '.csv':
                    df = pd.read_csv(raw_path, header=None)
                    df = self._process_df_headers(df)
                    csv_path = os.path.join(temp_dir, f"{safe_file_id}_Sheet1.csv")
                    df.to_csv(csv_path, index=False)
                    successful_paths.append(csv_path)
                    
                    view_name = f"file_{safe_file_id[:8]}_Sheet1"
                    self.con.execute(f"CREATE OR REPLACE VIEW {view_name} AS SELECT * FROM read_csv_auto('{csv_path}')")
                    mega_schema += f"- Table: `{view_name}` | Columns: {', '.join(list(df.columns)[:50])}\n"
                    
                else: # Excel
                    dfs = pd.read_excel(raw_path, sheet_name=None, header=None)
                    for sheet_name, df in dfs.items():
                        if df.empty: continue
                        
                        # Detect and split tables
                        tables = self._detect_and_split_tables(df)
                        
                        for t_idx, table_df in enumerate(tables):
                            if table_df.empty: continue
                            table_df = self._process_df_headers(table_df)
                            
                            # Sanitize sheet name for SQL
                            safe_sheet = re.sub(r'[^a-zA-Z0-9]', '_', str(sheet_name))
                            table_suffix = f"_t{t_idx}" if len(tables) > 1 else ""
                            csv_path = os.path.join(temp_dir, f"{safe_file_id}_{safe_sheet}{table_suffix}.csv")
                            table_df.to_csv(csv_path, index=False)
                            successful_paths.append(csv_path)
                            
                            view_name = f"file_{safe_file_id[:8]}_{safe_sheet}{table_suffix}"
                            self.con.execute(f"CREATE OR REPLACE VIEW {view_name} AS SELECT * FROM read_csv_auto('{csv_path}')")
                            mega_schema += f"- Table: `{view_name}` | Columns: {', '.join(list(table_df.columns)[:50])}\n"

                os.remove(raw_path) 
                mega_schema += "\n"
                
            except Exception as e:
                logger.error(f"Failed to process {file_id}: {e}")
                return f"Error processing file {file_id}: {str(e)}"

        return f"Files ready for analysis.\n\n{mega_schema}"

    def execute_sql(self, sql_query: str) -> Tuple[str, Optional[pd.DataFrame]]:
        """Executes DuckDB SQL and returns results summary and optional DataFrame."""
        try:
            logger.info(f"Executing SQL: {sql_query}")
            result = self.con.execute(sql_query)
            if result is None: return "Query executed successfully (0 rows).", None
            
            df = result.df()
            row_count = len(df)
            
            if row_count <= 10:
                return f"Query Success ({row_count} rows):\n{df.to_string()}", None
            else:
                summary = f"Query Success. Generated {row_count} rows. Data is available for charting via artifacts."
                return summary, df
        except Exception as e:
            return f"SQL Error: {str(e)}", None