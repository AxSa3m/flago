from time import monotonic
from typing import Any, cast
from urllib.parse import quote

import httpx

from fcgo.config import Settings
from fcgo.feishu.oauth import FeishuOAuthService, MissingOAuthScopeError
from fcgo.storage import SQLiteStore


class FeishuOpenAPI:
    def __init__(self, settings: Settings, store: SQLiteStore) -> None:
        self.settings = settings
        self.store = store
        self._tenant_token: str | None = None
        self._tenant_token_expires_at = 0.0

    async def get_doc_raw_content(self, document_id: str, actor_id: str) -> dict[str, Any]:
        return await self.get(
            f"/open-apis/docx/v1/documents/{document_id}/raw_content",
            actor_id=actor_id,
            require_user_token=True,
            required_scope_groups=(
                ("docx:document:readonly", "docx:document"),
            ),
        )

    async def get_wiki_node(self, wiki_token: str, actor_id: str) -> dict[str, Any]:
        return await self.get(
            "/open-apis/wiki/v2/spaces/get_node",
            actor_id=actor_id,
            params={"token": wiki_token},
            require_user_token=True,
            required_scope_groups=(
                ("wiki:node:read", "wiki:wiki:readonly", "wiki:wiki"),
            ),
        )

    async def read_sheet_range(
        self, spreadsheet_token: str, range_name: str, actor_id: str
    ) -> dict[str, Any]:
        encoded_range = quote(range_name, safe="")
        return await self.get(
            f"/open-apis/sheets/v2/spreadsheets/{spreadsheet_token}/values/{encoded_range}",
            actor_id=actor_id,
            params={
                "valueRenderOption": "ToString",
                "dateTimeRenderOption": "FormattedString",
            },
            require_user_token=True,
            required_scope_groups=(
                ("sheets:spreadsheet:readonly", "sheets:spreadsheet"),
            ),
        )

    async def query_sheet_metadata(self, spreadsheet_token: str, actor_id: str) -> dict[str, Any]:
        return await self.get(
            f"/open-apis/sheets/v3/spreadsheets/{spreadsheet_token}/sheets/query",
            actor_id=actor_id,
            require_user_token=True,
            required_scope_groups=(
                ("sheets:spreadsheet:readonly", "sheets:spreadsheet"),
            ),
        )

    async def list_bitable_tables(
        self,
        app_token: str,
        actor_id: str,
        *,
        limit: int = 200,
    ) -> dict[str, Any]:
        return await self.get(
            f"/open-apis/base/v3/bases/{app_token}/tables",
            actor_id=actor_id,
            params={"limit": limit, "offset": 0},
            require_user_token=True,
            required_scope_groups=(
                ("bitable:app:readonly", "bitable:app"),
            ),
        )

    async def list_bitable_views(
        self,
        app_token: str,
        table_id: str,
        actor_id: str,
        *,
        limit: int = 100,
    ) -> dict[str, Any]:
        return await self.get(
            f"/open-apis/base/v3/bases/{app_token}/tables/{table_id}/views",
            actor_id=actor_id,
            params={"limit": limit, "offset": 0},
            require_user_token=True,
            required_scope_groups=(
                ("bitable:app:readonly", "bitable:app"),
                ("base:view:read",),
            ),
        )

    async def list_bitable_fields(
        self,
        app_token: str,
        table_id: str,
        actor_id: str,
        *,
        limit: int = 100,
    ) -> dict[str, Any]:
        return await self.get(
            f"/open-apis/base/v3/bases/{app_token}/tables/{table_id}/fields",
            actor_id=actor_id,
            params={"limit": limit, "offset": 0},
            require_user_token=True,
            required_scope_groups=(
                ("bitable:app:readonly", "bitable:app"),
                ("base:field:read",),
            ),
        )

    async def list_bitable_records(
        self,
        app_token: str,
        table_id: str,
        actor_id: str,
        *,
        limit: int,
        view_id: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": limit, "offset": 0}
        if view_id:
            params["view_id"] = view_id
        return await self.get(
            f"/open-apis/base/v3/bases/{app_token}/tables/{table_id}/records",
            actor_id=actor_id,
            params=params,
            require_user_token=True,
            required_scope_groups=(
                ("bitable:app:readonly", "bitable:app"),
                ("base:record:read",),
            ),
        )

    async def search_bitable_records(
        self,
        app_token: str,
        table_id: str,
        actor_id: str,
        *,
        page_size: int,
        view_id: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"page_size": page_size}
        body: dict[str, Any] = {}
        if view_id:
            body["view_id"] = view_id
        return await self.post(
            f"/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records/search",
            actor_id=actor_id,
            json_body=body,
            params=params,
            require_user_token=True,
            required_scope_groups=(
                ("bitable:app:readonly", "bitable:app"),
                ("base:record:read",),
            ),
        )

    async def write_sheet_range(
        self,
        spreadsheet_token: str,
        range_name: str,
        values: list[list[Any]],
        actor_id: str,
    ) -> dict[str, Any]:
        return await self.put(
            f"/open-apis/sheets/v2/spreadsheets/{spreadsheet_token}/values",
            actor_id=actor_id,
            json_body={"valueRange": {"range": range_name, "values": values}},
            require_user_token=True,
            required_scope_groups=(
                ("sheets:spreadsheet", "sheets:spreadsheet:write"),
            ),
        )

    async def create_doc(
        self,
        title: str,
        actor_id: str,
        *,
        folder_token: str | None = None,
        content: str | None = None,
    ) -> dict[str, Any]:
        params = {"folder_token": folder_token} if folder_token else None
        document = await self.post(
            "/open-apis/docx/v1/documents",
            actor_id=actor_id,
            json_body={"title": title},
            params=params,
            require_user_token=True,
            required_scope_groups=(("docx:document", "docx:document:write"),),
        )
        document_id = str(
            document.get("document_id")
            or document.get("document", {}).get("document_id")
            or ""
        )
        if content and document_id:
            await self.append_doc_text(document_id, content, actor_id)
        return document

    async def append_doc_text(
        self,
        document_id: str,
        content: str,
        actor_id: str,
        *,
        block_id: str | None = None,
    ) -> dict[str, Any]:
        return await self.append_doc_blocks(
            document_id,
            _plain_text_blocks(content),
            actor_id,
            block_id=block_id,
        )

    async def append_doc_blocks(
        self,
        document_id: str,
        blocks: list[dict[str, Any]],
        actor_id: str,
        *,
        block_id: str | None = None,
        revision_id: int = -1,
        index: int = -1,
    ) -> dict[str, Any]:
        parent_block_id = block_id or document_id
        return await self.post(
            f"/open-apis/docx/v1/documents/{document_id}/blocks/{parent_block_id}/children",
            actor_id=actor_id,
            params={"document_revision_id": revision_id},
            json_body={"index": index, "children": blocks},
            require_user_token=True,
            required_scope_groups=(("docx:document", "docx:document:write"),),
        )

    async def create_bitable_record(
        self,
        app_token: str,
        table_id: str,
        fields: dict[str, Any],
        actor_id: str,
    ) -> dict[str, Any]:
        return await self.post(
            f"/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records",
            actor_id=actor_id,
            json_body={"fields": fields},
            require_user_token=True,
            required_scope_groups=(
                ("bitable:app",),
                ("base:record:create", "base:record:write", "base:record:read"),
            ),
        )

    async def update_bitable_record(
        self,
        app_token: str,
        table_id: str,
        record_id: str,
        fields: dict[str, Any],
        actor_id: str,
    ) -> dict[str, Any]:
        return await self.put(
            f"/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records/{record_id}",
            actor_id=actor_id,
            json_body={"fields": fields},
            require_user_token=True,
            required_scope_groups=(
                ("bitable:app",),
                ("base:record:update", "base:record:write", "base:record:read"),
            ),
        )

    async def delete_bitable_record(
        self,
        app_token: str,
        table_id: str,
        record_id: str,
        actor_id: str,
    ) -> dict[str, Any]:
        return await self.delete(
            f"/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records/{record_id}",
            actor_id=actor_id,
            require_user_token=True,
            required_scope_groups=(
                ("bitable:app",),
                ("base:record:delete", "base:record:write"),
            ),
        )

    async def get(
        self,
        path: str,
        *,
        actor_id: str,
        params: dict[str, Any] | None = None,
        require_user_token: bool = False,
        required_scope_groups: tuple[tuple[str, ...], ...] | None = None,
    ) -> dict[str, Any]:
        return await self._request(
            "GET",
            path,
            actor_id=actor_id,
            params=params,
            require_user_token=require_user_token,
            required_scope_groups=required_scope_groups,
        )

    async def post(
        self,
        path: str,
        *,
        actor_id: str,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        require_user_token: bool = False,
        required_scope_groups: tuple[tuple[str, ...], ...] | None = None,
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            path,
            actor_id=actor_id,
            json_body=json_body,
            params=params,
            require_user_token=require_user_token,
            required_scope_groups=required_scope_groups,
        )

    async def put(
        self,
        path: str,
        *,
        actor_id: str,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        require_user_token: bool = False,
        required_scope_groups: tuple[tuple[str, ...], ...] | None = None,
    ) -> dict[str, Any]:
        return await self._request(
            "PUT",
            path,
            actor_id=actor_id,
            json_body=json_body,
            params=params,
            require_user_token=require_user_token,
            required_scope_groups=required_scope_groups,
        )

    async def delete(
        self,
        path: str,
        *,
        actor_id: str,
        params: dict[str, Any] | None = None,
        require_user_token: bool = False,
        required_scope_groups: tuple[tuple[str, ...], ...] | None = None,
    ) -> dict[str, Any]:
        return await self._request(
            "DELETE",
            path,
            actor_id=actor_id,
            params=params,
            require_user_token=require_user_token,
            required_scope_groups=required_scope_groups,
        )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        actor_id: str,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        require_user_token: bool = False,
        required_scope_groups: tuple[tuple[str, ...], ...] | None = None,
    ) -> dict[str, Any]:
        token = await self._access_token(
            actor_id,
            require_user_token=require_user_token,
            required_scope_groups=required_scope_groups,
        )
        url = f"{self.settings.feishu_base_url.rstrip('/')}{path}"
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.request(
                method,
                url,
                headers={"Authorization": f"Bearer {token}"},
                json=json_body,
                params=params,
            )
        data = _response_json(response)
        if response.is_error:
            if isinstance(data, dict):
                raise RuntimeError(f"Feishu API error {data.get('code')}: {data.get('msg')}")
            response.raise_for_status()
        if not isinstance(data, dict):
            raise RuntimeError("Feishu API response is not JSON object")
        if data.get("code", 0) != 0:
            raise RuntimeError(f"Feishu API error {data.get('code')}: {data.get('msg')}")
        response_data = data.get("data", data)
        return cast(dict[str, Any], response_data)

    async def _access_token(
        self,
        actor_id: str,
        *,
        require_user_token: bool = False,
        required_scope_groups: tuple[tuple[str, ...], ...] | None = None,
    ) -> str:
        try:
            access_token = await FeishuOAuthService(
                self.settings,
                self.store,
            ).get_valid_access_token(
                actor_id,
                required_scope_groups=required_scope_groups,
            )
        except MissingOAuthScopeError as exc:
            raise RuntimeError(
                "当前飞书授权缺少所需权限："
                f"{', '.join(exc.missing_scopes)}。请在飞书中重新发送 /授权 并完成授权。"
            ) from exc
        if access_token:
            return access_token
        if require_user_token:
            raise RuntimeError("请先在飞书中发送 /授权 完成授权后再读取该资源")
        return await self._tenant_access_token()

    async def _tenant_access_token(self) -> str:
        if self._tenant_token and monotonic() < self._tenant_token_expires_at - 60:
            return self._tenant_token
        url = (
            f"{self.settings.feishu_base_url.rstrip('/')}"
            "/open-apis/auth/v3/tenant_access_token/internal"
        )
        payload = {
            "app_id": self.settings.feishu_app_id,
            "app_secret": self.settings.feishu_app_secret.get_secret_value(),
        }
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(url, json=payload)
        response.raise_for_status()
        data = cast(dict[str, Any], response.json())
        if data.get("code", 0) != 0:
            raise RuntimeError(f"Feishu tenant token error {data.get('code')}: {data.get('msg')}")
        self._tenant_token = data["tenant_access_token"]
        self._tenant_token_expires_at = monotonic() + float(data.get("expire", 7200))
        return self._tenant_token


def _response_json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return None


def _plain_text_blocks(content: str) -> list[dict[str, Any]]:
    lines = [line.strip() for line in content.replace("\r\n", "\n").split("\n")]
    non_empty_lines = [line for line in lines if line]
    if not non_empty_lines:
        non_empty_lines = [content.strip() or " "]
    return [
        {
            "block_type": 2,
            "text": {
                "elements": [
                    {
                        "text_run": {
                            "content": line,
                        }
                    }
                ]
            },
        }
        for line in non_empty_lines
    ]
