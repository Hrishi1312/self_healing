import requests
import base64
import urllib3
import ssl
from typing import Type
from pydantic import BaseModel, Field
from crewai.tools import BaseTool
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from urllib3.util.ssl_ import create_urllib3_context

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


# ─────────────────────────────────────────────────────────────
# CUSTOM SSL ADAPTER — Forces TLS 1.2, disables bad ciphers
# ─────────────────────────────────────────────────────────────

class ForceTLSAdapter(HTTPAdapter):
    """
    Custom HTTP Adapter that forces TLS 1.2 and relaxes
    SSL cipher restrictions to fix 'Connection aborted' errors
    behind corporate proxies or strict firewalls.
    """

    def init_poolmanager(self, *args, **kwargs):
        ctx = create_urllib3_context()
        ctx.check_hostname = False
        ctx.verify_mode    = ssl.CERT_NONE

        # ✅ Force TLS 1.2 — most compatible with Azure DevOps
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.maximum_version = ssl.TLSVersion.TLSv1_3

        # ✅ Use a broad cipher set to avoid handshake failures
        ctx.set_ciphers("DEFAULT@SECLEVEL=1")

        kwargs["ssl_context"] = ctx
        super().init_poolmanager(*args, **kwargs)

    def proxy_manager_for(self, proxy, **proxy_kwargs):
        ctx = create_urllib3_context()
        ctx.check_hostname = False
        ctx.verify_mode    = ssl.CERT_NONE
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.set_ciphers("DEFAULT@SECLEVEL=1")
        proxy_kwargs["ssl_context"] = ctx
        return super().proxy_manager_for(proxy, **proxy_kwargs)


# ─────────────────────────────────────────────────────────────
# SCHEMA
# ─────────────────────────────────────────────────────────────

class AzureWorkItemFetcherSchema(BaseModel):
    ado_org: str = Field(..., description="Azure DevOps organization name")
    ado_project: str = Field(..., description="Azure DevOps project name")
    ado_pat: str = Field(..., description="Personal Access Token")
    ado_work_item_type: str = Field(..., description="Work item type")
    user_story_id: str = Field(..., description="Work item ID")
    ado_area_path: str = Field(..., description="Area path")


# ─────────────────────────────────────────────────────────────
# TOOL
# ─────────────────────────────────────────────────────────────

class AzureWorkItemFetcher(BaseTool):
    name: str = "Azure Work Item Fetcher"
    description: str = "Fetches work item details from Azure DevOps."
    args_schema: Type[BaseModel] = AzureWorkItemFetcherSchema

    def _build_session(self, b64_credentials: str) -> requests.Session:
        """
        Builds a session with:
        - Forced TLS 1.2 via custom SSL adapter
        - Retry logic with exponential backoff
        - Required headers for Azure DevOps
        """
        session = requests.Session()

        # ✅ Use custom TLS adapter instead of default
        retry_strategy = Retry(
            total=3,
            backoff_factor=2,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET"],
            raise_on_status=False
        )

        tls_adapter = ForceTLSAdapter(max_retries=retry_strategy)

        session.mount("https://", tls_adapter)
        session.mount("http://",  tls_adapter)

        session.headers.update({
            "Content-Type":  "application/json",
            "Accept":        "application/json",
            "Authorization": f"Basic {b64_credentials}",
            "User-Agent":    "python-requests/2.31.0"
        })

        return session

    def _run(
        self,
        ado_org: str,
        ado_project: str,
        ado_pat: str,
        ado_work_item_type: str,
        user_story_id: str,
        ado_area_path: str
    ) -> str:

        session = None

        try:
            # ── Validate Work Item ID ─────────────────────────
            try:
                work_item_id = int(user_story_id)
            except ValueError:
                return f"Invalid work item ID: '{user_story_id}'. Must be a number."

            # ── Build Auth ────────────────────────────────────
            b64_credentials = base64.b64encode(
                f":{ado_pat}".encode("utf-8")
            ).decode("utf-8")

            # ── Build Session ─────────────────────────────────
            session = self._build_session(b64_credentials)

            # ── Build URL ─────────────────────────────────────
            url = (
                f"https://dev.azure.com/{ado_org}/{ado_project}"
                f"/_apis/wit/workitems/{work_item_id}"
                f"?api-version=7.0"
            )

            print(f"Requesting: {url}")

            # ── Make Request ──────────────────────────────────
            response = session.get(
                url,
                verify=False,
                timeout=(15, 45)  # connect=15s, read=45s
            )

            # ── Handle Status Codes ───────────────────────────
            if response.status_code == 203:
                # 203 means auth was ignored — PAT is likely wrong
                return (
                    "Received 203 Non-Authoritative response. "
                    "PAT token may be invalid or org name is incorrect. "
                    f"Org used: '{ado_org}'"
                )

            if response.status_code == 401:
                return (
                    "Authentication failed (401). "
                    "PAT token is expired or missing 'Work Items Read' scope."
                )

            if response.status_code == 403:
                return (
                    f"Access denied (403). "
                    f"PAT lacks permission for project: '{ado_project}'"
                )

            if response.status_code == 404:
                return (
                    f"Work item {work_item_id} not found (404). "
                    f"Verify org: '{ado_org}' and project: '{ado_project}'"
                )

            if response.status_code == 429:
                retry_after = response.headers.get("Retry-After", "60")
                return f"Rate limited. Retry after {retry_after} seconds."

            response.raise_for_status()

            # ── Parse JSON ────────────────────────────────────
            data   = response.json()
            fields = data.get("fields", {})

            # ── Validate Work Item Type ───────────────────────
            actual_type = fields.get("System.WorkItemType", "")
            if actual_type != ado_work_item_type:
                return (
                    f"Work item type mismatch. "
                    f"Expected: '{ado_work_item_type}', Found: '{actual_type}'"
                )

            # ── Validate Area Path ────────────────────────────
            actual_area = fields.get("System.AreaPath", "")
            if ado_area_path not in actual_area:
                return (
                    f"Area path mismatch. "
                    f"Expected: '{ado_area_path}', Found: '{actual_area}'"
                )

            # ── Extract Fields ────────────────────────────────
            description = (
                fields.get("System.Description") or "No description provided"
            )

            acceptance_criteria = (
                fields.get("Microsoft.VSTS.Common.AcceptanceCriteria")
            )
            if not acceptance_criteria:
                for key, value in fields.items():
                    if "acceptance" in key.lower() and value:
                        acceptance_criteria = value
                        break
            if not acceptance_criteria:
                acceptance_criteria = "No acceptance criteria defined"

            assigned_to = None
            if fields.get("System.AssignedTo"):
                assigned_to = fields["System.AssignedTo"].get("displayName")

            result = {
                "id":                  data.get("id"),
                "title":               fields.get("System.Title"),
                "description":         description,
                "acceptance_criteria": acceptance_criteria,
                "state":               fields.get("System.State"),
                "area_path":           fields.get("System.AreaPath"),
                "iteration_path":      fields.get("System.IterationPath"),
                "work_item_type":      fields.get("System.WorkItemType"),
                "assigned_to":         assigned_to,
                "tags":                fields.get("System.Tags"),
                "priority":            fields.get("Microsoft.VSTS.Common.Priority"),
                "created_by":          fields.get("System.CreatedBy", {}).get("displayName"),
                "created_date":        fields.get("System.CreatedDate"),
                "changed_date":        fields.get("System.ChangedDate"),
            }

            return f"Work item details: {result}"

        except requests.exceptions.SSLError as e:
            return (
                f"SSL Error persists even with TLS fix. "
                f"Your network may require a proxy. Details: {str(e)}"
            )

        except requests.exceptions.ConnectionError as e:
            return (
                f"Connection error to Azure DevOps. "
                f"Details: {str(e)}"
            )

        except requests.exceptions.Timeout:
            return "Request timed out. Azure DevOps did not respond in time."

        except requests.exceptions.RequestException as e:
            return f"Request error: {str(e)}"

        except Exception as e:
            return f"Unexpected error: {str(e)}"

        finally:
            if session:
                session.close()
