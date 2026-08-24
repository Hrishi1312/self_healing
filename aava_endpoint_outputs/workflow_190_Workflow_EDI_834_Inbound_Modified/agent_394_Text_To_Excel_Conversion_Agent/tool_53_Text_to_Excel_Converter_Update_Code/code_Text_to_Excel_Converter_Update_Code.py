import os
import io
import json
import re
import base64
import requests
import pandas as pd
from typing import Any, Type, Optional
from pydantic import BaseModel, Field
from crewai.tools import BaseTool

class UniversalTextToExcelConverterSchema(BaseModel):
    """Input schema for the Universal TextToExcelConverter tool."""
    text_content: str = Field(..., description="Raw text data extracted from the .txt file or stream.")
    github_token: str = Field(..., description="The user's personal GitHub Personal Access Token (PAT). Must have repo write permissions.")
    github_owner: str = Field(..., description="The GitHub account or organization owner name (e.g., 'username').")
    github_name: str = Field(..., description="The target repository name (e.g., 'repo_name').")
    github_branch: Optional[str] = Field(None, description="The target branch in the repository. If not provided, defaults to the repository's default branch.")
    output_filename: Optional[str] = Field(None, description="Preferred name for the generated Excel file. If not provided, defaults to 'output.xlsx'.")

class UniversalTextToExcelConverter(BaseTool):
    name: str = "universal_text_to_excel_converter"
    description: str = "Dynamically extracts multi-line headers, handles JSON blocks, and normalizes complex text structures into clean Excel sheets. Uploads the Excel file to a specified GitHub repository."
    args_schema: Type[BaseModel] = UniversalTextToExcelConverterSchema

    def _run(self, text_content: str, github_token: str, github_owner: str, github_name: str, github_branch: Optional[str] = None, output_filename: Optional[str] = None) -> dict:
        if not text_content or not text_content.strip():
            return {"status": "error", "download_url": None, "summary": "Input text content is empty."}
            
        cleaned_content = text_content.strip()
        github_repo = f"{github_owner}/{github_name}"
        
        # --- MALFORMED JSON HEURISTIC REPAIR BLOCK ---
        is_json_like = '"story_id"' in cleaned_content or '"user_story_id"' in cleaned_content or '"title"' in cleaned_content
        
        if is_json_like:
            try:
                if not cleaned_content.startswith('['):
                    if not cleaned_content.startswith('{'):
                        cleaned_content = "{\n" + cleaned_content
                    cleaned_content = "[\n" + cleaned_content
                
                if not cleaned_content.endswith(']'):
                    if not cleaned_content.endswith('}'):
                        cleaned_content = cleaned_content + "\n}"
                    cleaned_content = cleaned_content + "\n]"
                
                cleaned_content = re.sub(r'"\s*\{\s*"user_story_id"', '"}, {"user_story_id"', cleaned_content)
                
                json_data = json.loads(cleaned_content)
                if isinstance(json_data, dict):
                    json_data = [json_data]
                    
                df = pd.DataFrame(json_data)
                return self._upload_to_github(df, github_repo, github_token, github_branch, output_filename)
            except Exception:
                try:
                    records = []
                    current_record = {}
                    matches = re.findall(r'"([^"]+)":\s*"([^"]+)"', text_content)
                    
                    for key, val in matches:
                        if key in current_record or key in ["story_id", "user_story_id"] and current_record:
                            records.append(current_record)
                            current_record = {}
                        current_record[key] = val
                    if current_record:
                        records.append(current_record)
                        
                    if records:
                        df = pd.DataFrame(records)
                        return self._upload_to_github(df, github_repo, github_token, github_branch, output_filename)
                except Exception:
                    pass

        # --- BASELINE TABLE MATRIX LOGIC ---
        lines = cleaned_content.splitlines()
        sample_line = lines[0] if len(lines) > 0 else ""
        delimiter = '|' if '|' in sample_line else ','
        
        headers = []
        data_start_idx = 0
        
        for idx, line in enumerate(lines):
            line_str = line.strip()
            if not line_str or "content below is" in line_str.lower():
                continue
            if delimiter in line_str:
                if line_str.startswith(delimiter): line_str = line_str[1:]
                if line_str.endswith(delimiter): line_str = line_str[:-1]
                headers = [h.strip() for h in line_str.split(delimiter)]
                data_start_idx = idx + 1
                break

        if not headers:
            return {"status": "error", "download_url": None, "summary": "Could not identify a valid dynamic header matrix layout."}

        num_columns = len(headers)
        step_col_idx = -1
        for idx, h in enumerate(headers):
            normalized_h = h.lower()
            if "step #" in normalized_h or "step_num" in normalized_h or "step number" in normalized_h or h == "Step #":
                step_col_idx = idx
                break
        
        if step_col_idx == -1:
            step_col_idx = min(8, num_columns - 1)

        final_dataset = []
        parent_tracking_row = [""] * num_columns

        for line in lines[data_start_idx:]:
            line_str = line.strip()
            if not line_str or line_str.startswith("---") or line_str.startswith("___"):
                continue

            if delimiter not in line_str:
                if len(final_dataset) > 0:
                    final_dataset[-1][-1] = (final_dataset[-1][-1] + " " + line_str).strip()
                continue

            if line_str.startswith(delimiter): line_str = line_str[1:]
            if line_str.endswith(delimiter): line_str = line_str[:-1]

            parts = [p.strip() for p in line_str.split(delimiter)]
            
            while len(parts) < num_columns:
                parts.append("")
            if len(parts) > num_columns:
                parts = parts[:num_columns]

            has_parent_keys = any(parts[i] != "" for i in range(0, step_col_idx))
            
            if has_parent_keys:
                for i in range(0, step_col_idx):
                    parent_tracking_row[i] = parts[i]
                for i in range(step_col_idx, num_columns):
                    parent_tracking_row[i] = parts[i]
                final_dataset.append(list(parent_tracking_row))
                
            elif parts[step_col_idx] != "":
                for i in range(step_col_idx, num_columns):
                    parent_tracking_row[i] = parts[i]
                final_dataset.append(list(parent_tracking_row))
                
            elif len(final_dataset) > 0:
                text_append = " ".join([p for p in parts if p])
                if text_append:
                    final_dataset[-1][-1] = (final_dataset[-1][-1] + " " + text_append).strip()

        if final_dataset:
            df = pd.DataFrame(final_dataset, columns=headers)
        else:
            df = pd.DataFrame([["Layout conversion mapping error", "Verify content text formatting rules."]], columns=["Parsing Status", "Error Details"])

        return self._upload_to_github(df, github_repo, github_token, github_branch, output_filename)

    def _upload_to_github(self, df: pd.DataFrame, github_repo: str, github_token: str, github_branch: Optional[str], output_filename: Optional[str]) -> dict:
        """Helper method to execute GitHub storage write actions cleanly."""
        try:
            filename = (output_filename or 'output').replace(".xlsx", "") + ".xlsx"
            excel_buffer = io.BytesIO()
            df.to_excel(excel_buffer, index=False, engine='openpyxl')
            excel_buffer.seek(0)
            content_encoded = base64.b64encode(excel_buffer.read()).decode('utf-8')
            excel_buffer.close()

            url = f"https://api.github.com/repos/{github_repo}/contents/{filename}"
            headers_api = {
                "Authorization": f"token {github_token}",
                "Accept": "application/vnd.github.v3+json"
            }
            
            sha = None
            # Append branch query to see if the file/branch exists, if branch is provided
            get_url = f"{url}?ref={github_branch}" if github_branch else url
            get_res = requests.get(get_url, headers=headers_api)
            
            if get_res.status_code == 200:
                sha = get_res.json().get("sha")

            data_payload = {
                "message": f"Automated Dynamic Schema Matrix Transformation: {filename}",
                "content": content_encoded
            }
            
            # If a branch is explicitly provided, add it to payload. Otherwise GitHub uses default branch.
            if github_branch:
                data_payload["branch"] = github_branch
                
            if sha:
                data_payload["sha"] = sha

            put_res = requests.put(url, headers=headers_api, json=data_payload)
            
            if put_res.status_code in [200, 201]:
                res_data = put_res.json()
                
                # Fetch dynamically generated URLs from GitHub's response
                content_info = res_data.get("content", {})
                download_url = content_info.get("download_url")
                
                return {
                    "status": "success",
                    "download_url": download_url,
                    "summary": f"Excel spreadsheet cleanly created with {df.shape[0]} rows and {df.shape[1]} columns."
                }
            else:
                return {
                    "status": "error", 
                    "download_url": None, 
                    "summary": f"GitHub API rejection ({put_res.status_code}): Ensure repo exists, is initialized with a commit, and token has full 'repo' write scopes. Details: {put_res.text}"
                }
            
        except Exception as e:
            return {"status": "error", "download_url": None, "summary": f"Transformation Exception occurred: {str(e)}"}