"""
=============================================================================
File: engine.py
Capabilities:
1. Object-Oriented State: Tracks file formats (CSV vs XLSX) across AI turns.
2. Background CSV Extraction: Converts Excel tabs to CSVs for DuckDB disk speed.
3. Multi-File JOINs: Registers multiple files into a single DuckDB context.
4. SQL Sanitization: Handles hyphens and special characters in File IDs.
5. Dynamic GCS Signing: Generates secure V4 download links via Metadata identity.
6. Serverless State Persistence: Rehydrates DuckDB views from Parquet files in /tmp/.
7. Multi-Tab CSV Optimization: Uses Google Sheets v4 API to stream raw CSVs instantly.
8. Zero-Copy DuckDB Registration: Direct memory binding of Pandas DataFrames.
9. High-Performance Rust Excel Parsing: Uses python-calamine with openpyxl fallback.
10. Fast-Path Early Exit: Bypasses sub-table splitting for massive sheets (>2000 rows).
11. Local File Support: Discovers and directly registers local workspace files.
=============================================================================
"""

import os
import glob
import tempfile
import duckdb
import pandas as pd
import uuid
import re
import datetime
import time
import requests # Standard requests for Metadata
from typing import Dict, List, Tuple, Optional
from googleapiclient.http import MediaIoBaseDownload

# Absolute import for root-level deployment (Important for Cloud Run/ADK)
try:
    from .config import get_logger
except ImportError:
    from config import get_logger

logger = get_logger(__name__)

class SheetDataEngine:
    def __init__(self):
        # In-memory connection avoids "Conflicting Lock" errors in serverless environments
        self.con = duckdb.connect(':memory:')
        # Hardening: Cap DuckDB memory to prevent container OOM SIGKILL and restrict worker threads
        self.con.execute("SET memory_limit = '256MB'")
        self.con.execute("SET threads = 2")
        self.original_formats = {}  # Format: { 'file_id': '.xlsx' }
        self.registered_tables = []
        self.session = requests.Session()
        logger.info("SheetDataEngine initialized in-memory with serverless memory_limit='256MB', threads=2, and persistent HTTP session.")
        self._rehydrate_parquet_views()

    def _rehydrate_parquet_views(self):
        """Serverless State Persistence: Rehydrates DuckDB views from Parquet and CSV files in /tmp/."""
        temp_dir = tempfile.gettempdir()
        parquet_files = glob.glob(os.path.join(temp_dir, "rehydrate_*.parquet"))
        csv_files = glob.glob(os.path.join(temp_dir, "rehydrate_*.csv"))
        
        for path in parquet_files:
            try:
                vname = os.path.basename(path).replace("rehydrate_", "").replace(".parquet", "")
                self.con.execute(f"CREATE OR REPLACE VIEW {vname} AS SELECT * FROM read_parquet('{path}')")
                if vname not in self.registered_tables:
                    self.registered_tables.append(vname)
                logger.info(f"Rehydrated view '{vname}' from Parquet {path}")
            except Exception as e:
                logger.warning(f"Failed to rehydrate view from Parquet {path}: {e}")
                
        for path in csv_files:
            try:
                vname = os.path.basename(path).replace("rehydrate_", "").replace(".csv", "")
                self.con.execute(f"CREATE OR REPLACE VIEW {vname} AS SELECT * FROM read_csv_auto('{path}', sample_size=1000)")
                if vname not in self.registered_tables:
                    self.registered_tables.append(vname)
                logger.info(f"Rehydrated view '{vname}' from CSV {path}")
            except Exception as e:
                logger.warning(f"Failed to rehydrate view from CSV {path}: {e}")

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
        # Fast-Path Table Detection: bypass O(R*C) scan for large dataframes
        if df.shape[0] > 2000 or df.shape[1] > 50:
            logger.info(f"Fast-path enabled for sheet with {df.shape[0]} rows and {df.shape[1]} cols. Skipping multi-table split.")
            return [df]

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

    def _register_and_persist(self, view_name: str, df: pd.DataFrame) -> None:
        """Zero-Copy registration in DuckDB and persistence to Parquet."""
        temp_dir = tempfile.gettempdir()
        t_start = time.perf_counter()
        try:
            # Zero-Copy registration
            self.con.register(view_name, df)
            if view_name not in self.registered_tables:
                self.registered_tables.append(view_name)
            
            # Serverless persistence to Parquet
            parquet_path = os.path.join(temp_dir, f"rehydrate_{view_name}.parquet")
            self.con.execute(f"COPY {view_name} TO '{parquet_path}' (FORMAT PARQUET)")
            t_elapsed = time.perf_counter() - t_start
            p_mb = os.path.getsize(parquet_path) / (1024 * 1024) if os.path.exists(parquet_path) else 0
            logger.info(f"[TIMER] Registered & persisted '{view_name}' (Zero-Copy) in {t_elapsed:.3f}s | Rows: {len(df)} | Parquet: {p_mb:.2f} MB")
        except Exception as e:
            logger.error(f"Error registering/persisting view '{view_name}': {e}")

    def _register_and_persist_csv(self, view_name: str, csv_path: str) -> List[Tuple[str, List[str]]]:
        """Dual-Path CSV Ingestion: Zero-Pandas DuckDB COPY for massive files vs Pandas Multi-Table split for standard sheets."""
        temp_dir = tempfile.gettempdir()
        t_start = time.perf_counter()
        logger.info(f"[_register_and_persist_csv] Ingesting CSV for view: '{view_name}' from: '{csv_path}'")
        try:
            csv_size_mb = os.path.getsize(csv_path) / (1024 * 1024) if os.path.exists(csv_path) else 0
            logger.info(f"[_register_and_persist_csv] CSV source file size: {csv_size_mb:.2f} MB")
            
            # Normal Sheets / Smaller Files (<=15MB): Multi-table sub-splitting and header processing
            if csv_size_mb <= 15.0:
                logger.info(f"[_register_and_persist_csv] File is <=15MB. Executing Multi-Table detection and header processing via Pandas...")
                dfs = pd.read_csv(csv_path, header=None)
                tables = self._detect_and_split_tables(dfs)
                
                table_infos = []
                for t_idx, table_df in enumerate(tables):
                    if table_df.empty: continue
                    table_df = self._process_df_headers(table_df)
                    table_suffix = f"_t{t_idx}" if len(tables) > 1 else ""
                    final_vname = f"{view_name}{table_suffix}"
                    self._register_and_persist(final_vname, table_df)
                    table_infos.append((final_vname, [str(c) for c in table_df.columns]))
                logger.info(f"[_register_and_persist_csv] Successfully processed {len(tables)} sub-table(s) in {time.perf_counter() - t_start:.3f}s")
                return table_infos

            # Massive Files (>15MB): Extremely fast Zero-COPY View Binding and Direct CSV Persistence
            persist_csv_path = os.path.join(temp_dir, f"rehydrate_{view_name}.csv")
            logger.info(f"[_register_and_persist_csv] Massive file (>15MB). Direct Copying to persistent path: '{persist_csv_path}'...")
            import shutil
            shutil.copy(csv_path, persist_csv_path)
            
            # Register view directly on the persistent CSV file without executing any RAM-heavy Parquet COPY
            logger.info(f"[_register_and_persist_csv] Binding DuckDB VIEW directly to persistent CSV file...")
            self.con.execute(f"CREATE OR REPLACE VIEW {view_name} AS SELECT * FROM read_csv_auto('{persist_csv_path}', sample_size=1000)")
            if view_name not in self.registered_tables:
                self.registered_tables.append(view_name)
            
            # Fetch columns for schema summary
            logger.info(f"[_register_and_persist_csv] Describing view schema...")
            cols_res = self.con.execute(f"DESCRIBE {view_name}").fetchall()
            t_elapsed = time.perf_counter() - t_start
            c_mb = os.path.getsize(persist_csv_path) / (1024 * 1024) if os.path.exists(persist_csv_path) else 0
            logger.info(f"[TIMER] Registered & persisted '{view_name}' (Zero-COPY View) in {t_elapsed:.3f}s | Columns: {len(cols_res)} | Size: {c_mb:.2f} MB")
            return [(view_name, [str(row[0]) for row in cols_res])]
        except Exception as e:
            logger.error(f"Error in direct CSV ingestion for '{view_name}': {e}. Falling back to standard Pandas single-table ingestion...")
            try:
                pandas_start = time.perf_counter()
                df = pd.read_csv(csv_path)
                logger.info(f"[_register_and_persist_csv] Fallback Pandas read_csv loaded {len(df)} rows in {time.perf_counter() - pandas_start:.3f}s")
                df = self._process_df_headers(df)
                self._register_and_persist(view_name, df)
                return [(view_name, [str(c) for c in df.columns])]
            except Exception as pandas_exc:
                logger.error(f"Fatal error in Fallback Pandas ingestion for '{view_name}': {pandas_exc}")
                raise

    def download_and_extract(self, file_identifiers: str, drive_service) -> str:
        """Downloads files, sanitizes names for SQL, and builds a mega-schema."""
        pipe_start = time.perf_counter()
        ids = [i.strip() for i in file_identifiers.split(',')]
        temp_dir = tempfile.gettempdir()
        mega_schema = "MEGA-SCHEMA FOR REQUESTED FILES:\n\n"

        for url_or_id in ids:
            # First, check if url_or_id is a local file path
            local_path = None
            if os.path.exists(url_or_id):
                local_path = url_or_id
            elif os.path.exists(os.path.join(os.getcwd(), url_or_id)):
                local_path = os.path.join(os.getcwd(), url_or_id)
            elif os.path.exists(os.path.basename(url_or_id)):
                local_path = os.path.basename(url_or_id)
                
            if not local_path:
                # Check for files in parent or workspace
                workspace_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
                candidate1 = os.path.join(workspace_dir, url_or_id)
                candidate2 = os.path.join(workspace_dir, os.path.basename(url_or_id))
                if os.path.exists(candidate1):
                    local_path = candidate1
                elif os.path.exists(candidate2):
                    local_path = candidate2

            if local_path:
                original_name = os.path.basename(local_path)
                ext = os.path.splitext(original_name)[1].lower()
                safe_file_id = re.sub(r'[^a-zA-Z0-9]', '_', os.path.splitext(original_name)[0])
                self.original_formats[safe_file_id] = ext
                logger.info(f"Processing local file {original_name}...")
                mega_schema += f"--- Source File: {original_name} (Local) ---\n"
                
                if ext == '.csv':
                    view_name = f"file_{safe_file_id}_Sheet1"
                    table_infos = self._register_and_persist_csv(view_name, local_path)
                    for vname, cols in table_infos:
                        mega_schema += f"- Table: `{vname}` | Columns: {', '.join(cols[:50])}\n\n"
                    continue
                elif ext in ['.xlsx', '.xls']:
                    try:
                        # Try high-performance Calamine engine first
                        logger.info(f"Attempting to parse {original_name} using calamine engine...")
                        dfs = pd.read_excel(local_path, sheet_name=None, header=None, engine='calamine')
                        logger.info(f"Parsed Excel {original_name} using high-performance 'calamine' engine.")
                    except Exception as ce:
                        # Graceful fallback to openpyxl
                        logger.warning(f"Calamine engine not available or failed for {original_name}: {ce}. Falling back to openpyxl...")
                        # TODO(security): Implement XML hardening for openpyxl fallback (e.g., defusedxml) to disable external entity expansion and DTD processing.
                        dfs = pd.read_excel(local_path, sheet_name=None, header=None)
                    
                    for sheet_name, df in dfs.items():
                        if df.empty: continue
                        tables = self._detect_and_split_tables(df)
                        for t_idx, table_df in enumerate(tables):
                            if table_df.empty: continue
                            table_df = self._process_df_headers(table_df)
                            safe_sheet = re.sub(r'[^a-zA-Z0-9]', '_', str(sheet_name))
                            table_suffix = f"_t{t_idx}" if len(tables) > 1 else ""
                            view_name = f"file_{safe_file_id}_{safe_sheet}{table_suffix}"
                            self._register_and_persist(view_name, table_df)
                            mega_schema += f"- Table: `{view_name}` | Columns: {', '.join(list(table_df.columns)[:50])}\n"
                    mega_schema += "\n"
                    continue
                else:
                    logger.warning(f"File {original_name} is not supported. Ext: {ext}")
                    return f"Error: File '{original_name}' is not a supported Google Sheet, Excel, or CSV file."

            # Drive API Path
            match = re.search(r"/(?:d|folders)/([a-zA-Z0-9_-]+)", url_or_id)
            file_id = match.group(1) if match else url_or_id
            
            # SANITIZATION: Replace hyphens and special chars with underscores for SQL compliance
            safe_file_id = re.sub(r'[^a-zA-Z0-9]', '_', file_id)
            
            try:
                meta = drive_service.files().get(fileId=file_id, fields='name, mimeType, resourceKey', supportsAllDrives=True).execute()
                mime = meta.get('mimeType')
                original_name = meta.get('name', 'Unknown')
                resource_key = meta.get('resourceKey')
                
                supported_mimes = [
                    'application/vnd.google-apps.spreadsheet',
                    'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                    'application/vnd.ms-excel',
                    'text/csv'
                ]
                if mime not in supported_mimes:
                    logger.warning(f"File {original_name} is not supported. Mime: {mime}")
                    return f"Error: File '{original_name}' is not a supported Google Sheet, Excel, or CSV file."

                # Multi-Tab CSV Optimization for Google Sheets
                if mime == 'application/vnd.google-apps.spreadsheet' and hasattr(drive_service, 'access_token') and drive_service.access_token:
                    logger.info(f"Using Multi-Tab CSV Optimization for Google Sheet: {original_name}")
                    self.original_formats[file_id] = '.csv'
                    mega_schema += f"--- Source File: {original_name} (ID: {file_id}) [Multi-Tab CSV Stream] ---\n"
                    
                    headers = {'Authorization': f'Bearer {drive_service.access_token}'}
                    if resource_key:
                        headers['X-Goog-Drive-Resource-Keys'] = f"{file_id}/{resource_key}"
                        
                    sheets_api_url = f"https://sheets.googleapis.com/v4/spreadsheets/{file_id}?fields=sheets.properties(sheetId,title)"
                    try:
                        meta_start = time.perf_counter()
                        res = self.session.get(sheets_api_url, headers=headers, timeout=(10, 30))
                        meta_elapsed = time.perf_counter() - meta_start
                        logger.info(f"[TIMER] Sheets API properties lookup for '{original_name}' completed in {meta_elapsed:.3f}s")
                        
                        if res.status_code == 200:
                            sheets_data = res.json().get('sheets', [])
                            for sheet in sheets_data:
                                props = sheet.get('properties', {})
                                sheet_id = props.get('sheetId')
                                sheet_title = props.get('title', 'Sheet')
                                
                                logger.info(f"[CSV Stream] Requesting raw CSV for tab: '{sheet_title}' (GID: {sheet_id})...")
                                tab_start = time.perf_counter()
                                csv_export_url = f"https://docs.google.com/spreadsheets/d/{file_id}/export?format=csv&gid={sheet_id}"
                                try:
                                    logger.info(f"[CSV Stream] Sending GET request to export API (stream=True)...")
                                    csv_res = self.session.get(csv_export_url, headers=headers, stream=True, timeout=(10, 60))
                                    logger.info(f"[CSV Stream] Response headers received. Status Code: {csv_res.status_code} | Content-Type: {csv_res.headers.get('Content-Type')} | Content-Length: {csv_res.headers.get('Content-Length')}")
                                    
                                    if csv_res.status_code == 200:
                                        tab_raw_path = os.path.join(temp_dir, f"tab_{safe_file_id}_{sheet_id}.csv")
                                        logger.info(f"[CSV Stream] Downloading chunks to temporary path: {tab_raw_path}")
                                        total_bytes_downloaded = 0
                                        chunk_count = 0
                                        with open(tab_raw_path, 'wb') as fh:
                                            for chunk in csv_res.iter_content(chunk_size=1024 * 1024 * 5):
                                                if chunk:
                                                    fh.write(chunk)
                                                    chunk_len = len(chunk)
                                                    total_bytes_downloaded += chunk_len
                                                    chunk_count += 1
                                                    logger.info(f"[CSV Stream Progress] Downloaded chunk #{chunk_count} ({chunk_len / (1024*1024):.2f} MB) | Total: {total_bytes_downloaded / (1024*1024):.2f} MB")
                                        
                                        if os.path.exists(tab_raw_path):
                                            file_size_mb = os.path.getsize(tab_raw_path) / (1024 * 1024)
                                            logger.info(f"[CSV Stream Success] File written successfully. Total size: {file_size_mb:.2f} MB")
                                            if file_size_mb > 0:
                                                safe_sheet = re.sub(r'[^a-zA-Z0-9]', '_', str(sheet_title))
                                                view_name = f"file_{safe_file_id}_{safe_sheet}"
                                                table_infos = self._register_and_persist_csv(view_name, tab_raw_path)
                                                for vname, cols in table_infos:
                                                    mega_schema += f"- Table: `{vname}` | Columns: {', '.join(cols[:50])}\n"
                                                logger.info(f"[CSV Stream Cleanup] Removing temporary CSV file: {tab_raw_path}")
                                                os.remove(tab_raw_path)
                                            else:
                                                logger.warning(f"[CSV Stream Warning] Downloaded CSV file is 0 bytes for tab '{sheet_title}'.")
                                    else:
                                        err_preview = csv_res.text[:500] if hasattr(csv_res, 'text') else "No text"
                                        logger.error(f"[CSV Stream Error] Export API returned non-200 status: {csv_res.status_code}. Response: {err_preview}")
                                except Exception as req_exc:
                                    logger.error(f"[CSV Stream Exception] Exception while downloading/streaming CSV for tab '{sheet_title}': {req_exc}", exc_info=True)
                                    raise
                                
                                tab_elapsed = time.perf_counter() - tab_start
                                logger.info(f"[TIMER] Tab '{sheet_title}' CSV extraction & DuckDB ingestion completed in {tab_elapsed:.3f}s")
                            mega_schema += "\n"
                            continue
                    except Exception as sheet_exc:
                        logger.warning(f"Multi-Tab CSV Streaming failed: {sheet_exc}. Falling back to standard XLSX export...")

                ext = ".xlsx" if "spreadsheet" in mime or "excel" in mime else ".csv"
                self.original_formats[file_id] = ext
                
                raw_path = os.path.join(temp_dir, f"raw_{safe_file_id}{ext}")
                request = drive_service.files().export_media(fileId=file_id, mimeType='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet') if 'google-apps.spreadsheet' in mime else drive_service.files().get_media(fileId=file_id, supportsAllDrives=True)
                
                logger.info(f"Downloading {original_name}...")
                dl_start = time.perf_counter()
                with open(raw_path, 'wb') as fh:
                    downloader = MediaIoBaseDownload(fh, request, chunksize=1024 * 1024 * 10)
                    done = False
                    while not done:
                        _, done = downloader.next_chunk()
                dl_elapsed = time.perf_counter() - dl_start
                file_mb = os.path.getsize(raw_path) / (1024 * 1024) if os.path.exists(raw_path) else 0
                throughput = file_mb / dl_elapsed if dl_elapsed > 0 else 0
                logger.info(f"[TIMER] Download '{original_name}' completed in {dl_elapsed:.3f}s | Size: {file_mb:.2f} MB | Speed: {throughput:.2f} MB/s")

                mega_schema += f"--- Source File: {original_name} (ID: {file_id}) ---\n"
                
                if ext == '.csv':
                    view_name = f"file_{safe_file_id}_Sheet1"
                    table_infos = self._register_and_persist_csv(view_name, raw_path)
                    for vname, cols in table_infos:
                        mega_schema += f"- Table: `{vname}` | Columns: {', '.join(cols[:50])}\n"
                    
                else: # Excel
                    try:
                        parse_start = time.perf_counter()
                        logger.info(f"Attempting to parse {original_name} using calamine engine...")
                        dfs = pd.read_excel(raw_path, sheet_name=None, header=None, engine='calamine')
                        parse_elapsed = time.perf_counter() - parse_start
                        logger.info(f"[TIMER] High-performance 'calamine' Rust Excel parsing completed in {parse_elapsed:.3f}s | Sheets: {len(dfs)}")
                    except Exception as ce:
                        logger.warning(f"Calamine engine not available or failed for {original_name}: {ce}. Falling back to openpyxl...")
                        parse_start = time.perf_counter()
                        dfs = pd.read_excel(raw_path, sheet_name=None, header=None)
                        parse_elapsed = time.perf_counter() - parse_start
                        logger.info(f"[TIMER] Standard 'openpyxl' Excel parsing completed in {parse_elapsed:.3f}s | Sheets: {len(dfs)}")

                    split_start = time.perf_counter()
                    for sheet_name, df in dfs.items():
                        if df.empty: continue
                        tables = self._detect_and_split_tables(df)
                        for t_idx, table_df in enumerate(tables):
                            if table_df.empty: continue
                            table_df = self._process_df_headers(table_df)
                            safe_sheet = re.sub(r'[^a-zA-Z0-9]', '_', str(sheet_name))
                            table_suffix = f"_t{t_idx}" if len(tables) > 1 else ""
                            view_name = f"file_{safe_file_id}_{safe_sheet}{table_suffix}"
                            self._register_and_persist(view_name, table_df)
                            mega_schema += f"- Table: `{view_name}` | Columns: {', '.join(list(table_df.columns)[:50])}\n"
                    split_elapsed = time.perf_counter() - split_start
                    logger.info(f"[TIMER] Table splitting and registration completed in {split_elapsed:.3f}s")

                os.remove(raw_path) 
                mega_schema += "\n"
                
            except Exception as e:
                logger.error(f"Failed to process {file_id}: {e}")
                return f"Error processing file {file_id}: {str(e)}"

        pipe_elapsed = time.perf_counter() - pipe_start
        logger.info(f"[TIMER] Total download_and_extract pipeline for {len(ids)} file(s) completed in {pipe_elapsed:.3f}s")
        return f"Files ready for analysis.\n\n{mega_schema}"

    def execute_sql(self, sql_query: str) -> Tuple[str, Optional[pd.DataFrame]]:
        """Executes DuckDB SQL and returns results summary and optional DataFrame."""
        sql_start = time.perf_counter()
        try:
            logger.info(f"Executing SQL: {sql_query}")
            result = self.con.execute(sql_query)
            sql_elapsed = time.perf_counter() - sql_start
            logger.info(f"[TIMER] DuckDB SQL execution completed in {sql_elapsed:.3f}s")
            
            if result is None: return "Query executed successfully (0 rows).", None
            
            df = result.df()
            row_count = len(df)
            
            if row_count <= 10:
                return f"Query Success ({row_count} rows):\n{df.to_string()}", None
            else:
                summary = f"Query Success. Generated {row_count} rows. Data is available for charting via artifacts."
                return summary, df
        except Exception as e:
            sql_elapsed = time.perf_counter() - sql_start
            logger.error(f"[TIMER] DuckDB SQL execution failed in {sql_elapsed:.3f}s: {e}")
            return f"SQL Error: {str(e)}", None
