import re
from dataclasses import dataclass
from time import monotonic
from typing import Any, cast
from urllib.parse import quote, unquote

import httpx

from flago.config import Settings
from flago.feishu.oauth import FeishuOAuthService, MissingOAuthScopeError
from flago.models import AuditEventType
from flago.storage import SQLiteStore


@dataclass(frozen=True)
class DownloadedFile:
    content: bytes
    content_type: str
    filename: str | None


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

    async def list_doc_blocks(
        self,
        document_id: str,
        actor_id: str,
        *,
        max_items: int,
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        page_token: str | None = None
        while len(items) < max(max_items, 0):
            params: dict[str, Any] = {
                "page_size": min(500, max(max_items - len(items), 1)),
                "document_revision_id": -1,
            }
            if page_token:
                params["page_token"] = page_token
            data = await self.get(
                f"/open-apis/docx/v1/documents/{document_id}/blocks",
                actor_id=actor_id,
                params=params,
                require_user_token=True,
                required_scope_groups=(
                    ("docx:document:readonly", "docx:document"),
                ),
            )
            page_items = data.get("items")
            if not isinstance(page_items, list):
                break
            items.extend(item for item in page_items if isinstance(item, dict))
            if not data.get("has_more"):
                break
            next_page_token = data.get("page_token")
            if not next_page_token:
                break
            page_token = str(next_page_token)
        return items[: max(max_items, 0)]

    async def download_doc_media(
        self,
        file_token: str,
        actor_id: str,
        *,
        max_bytes: int,
    ) -> DownloadedFile:
        return await self._download_binary(
            f"/open-apis/drive/v1/medias/{file_token}/download",
            actor_id=actor_id,
            max_bytes=max_bytes,
            required_scope_groups=(
                (
                    "docs:document.media:download",
                    "drive:drive:readonly",
                    "drive:drive",
                ),
            ),
        )

    async def download_message_resource(
        self,
        message_id: str,
        file_key: str,
        *,
        resource_type: str = "image",
        max_bytes: int,
    ) -> DownloadedFile:
        encoded_message_id = quote(message_id, safe="")
        encoded_file_key = quote(file_key, safe="")
        return await self._download_tenant_binary(
            f"/open-apis/im/v1/messages/{encoded_message_id}/resources/{encoded_file_key}"
            f"?type={quote(resource_type, safe='')}",
            max_bytes=max_bytes,
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
            f"/open-apis/bitable/v1/apps/{app_token}/tables",
            actor_id=actor_id,
            params={"page_size": min(max(limit, 1), 100)},
            require_user_token=True,
            required_scope_groups=(
                ("base:table:read", "bitable:app:readonly", "bitable:app"),
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
            f"/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/views",
            actor_id=actor_id,
            params={"page_size": min(max(limit, 1), 100)},
            require_user_token=True,
            required_scope_groups=(
                ("base:view:read", "bitable:app:readonly", "bitable:app"),
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
            f"/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/fields",
            actor_id=actor_id,
            params={"page_size": min(max(limit, 1), 100)},
            require_user_token=True,
            required_scope_groups=(
                ("base:field:read", "bitable:app:readonly", "bitable:app"),
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
        params: dict[str, Any] = {"page_size": min(max(limit, 1), 500)}
        if view_id:
            params["view_id"] = view_id
        return await self.get(
            f"/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records",
            actor_id=actor_id,
            params=params,
            require_user_token=True,
            required_scope_groups=(
                ("base:record:read", "bitable:app:readonly", "bitable:app"),
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

    async def search_docs(
        self,
        query: str,
        actor_id: str,
        *,
        count: int,
        offset: int = 0,
        docs_types: list[str] | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "search_key": query,
            "count": min(max(count, 1), 50),
            "offset": max(offset, 0),
            "docs_types": docs_types or ["doc", "sheet", "bitable"],
        }
        data = await self.post(
            "/open-apis/suite/docs-api/search/object",
            actor_id=actor_id,
            json_body=body,
            require_user_token=True,
            required_scope_groups=(
                (
                    "drive:drive.search:readonly",
                    "search:docs:read",
                    "drive:drive:readonly",
                    "drive:drive",
                ),
            ),
        )
        entities = data.get("docs_entities")
        result_count = len(entities) if isinstance(entities, list) else 0
        await self.store.audit(
            AuditEventType.RESOURCE_SEARCHED,
            actor_id=actor_id,
            detail={
                "query_length": len(query),
                "count": body["count"],
                "offset": body["offset"],
                "docs_types": body["docs_types"],
                "result_count": result_count,
                "has_more": bool(data.get("has_more")),
            },
        )
        return data

    async def search_wiki_nodes(
        self,
        query: str,
        actor_id: str,
        *,
        page_size: int,
        page_token: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"page_size": min(max(page_size, 1), 50)}
        if page_token:
            params["page_token"] = page_token
        data = await self.post(
            "/open-apis/wiki/v2/nodes/search",
            actor_id=actor_id,
            params=params,
            json_body={"query": query[:50]},
            require_user_token=True,
            required_scope_groups=(
                ("wiki:wiki:readonly", "wiki:wiki", "wiki:node:read"),
            ),
        )
        items = data.get("items")
        await self.store.audit(
            AuditEventType.RESOURCE_SEARCHED,
            actor_id=actor_id,
            detail={
                "source": "wiki",
                "query_length": len(query),
                "page_size": params["page_size"],
                "result_count": len(items) if isinstance(items, list) else 0,
                "has_more": bool(data.get("has_more")),
            },
        )
        return data

    async def list_recent_messages(
        self,
        *,
        actor_id: str,
        container_id_type: str,
        container_id: str,
        start_time: Any,
        end_time: Any,
        page_size: int,
    ) -> dict[str, Any]:
        return await self.get(
            "/open-apis/im/v1/messages",
            actor_id="",
            params={
                "container_id_type": container_id_type,
                "container_id": container_id,
                "start_time": _unix_seconds(start_time),
                "end_time": _unix_seconds(end_time),
                "sort_type": "ByCreateTimeDesc",
                "page_size": min(max(page_size, 1), 50),
            },
            require_user_token=False,
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
        index: int = -1,
    ) -> dict[str, Any]:
        return await self.append_doc_blocks(
            document_id,
            _plain_text_blocks(content),
            actor_id,
            block_id=block_id,
            index=index,
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

    async def delete_doc_block(
        self,
        document_id: str,
        block_id: str,
        actor_id: str,
        *,
        revision_id: int = -1,
    ) -> dict[str, Any]:
        return await self.put(
            f"/open-apis/docs_ai/v1/documents/{document_id}",
            actor_id=actor_id,
            json_body={
                "command": "block_delete",
                "block_id": block_id,
                "format": "xml",
                "revision_id": revision_id,
            },
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
        async with httpx.AsyncClient(
            timeout=30,
            proxy=self.settings.feishu_http_proxy,
        ) as client:
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

    async def _download_binary(
        self,
        path: str,
        *,
        actor_id: str,
        max_bytes: int,
        required_scope_groups: tuple[tuple[str, ...], ...],
    ) -> DownloadedFile:
        token = await self._access_token(
            actor_id,
            require_user_token=True,
            required_scope_groups=required_scope_groups,
        )
        url = f"{self.settings.feishu_base_url.rstrip('/')}{path}"
        async with (
            httpx.AsyncClient(
                timeout=30,
                proxy=self.settings.feishu_http_proxy,
            ) as client,
            client.stream(
                "GET",
                url,
                headers={"Authorization": f"Bearer {token}"},
            ) as response,
        ):
            if response.is_error:
                body = await response.aread()
                detail = _binary_error_detail(body)
                raise RuntimeError(
                    f"Feishu API error HTTP {response.status_code}: {detail}"
                )
            content_length = _int_or_none(response.headers.get("content-length"))
            if content_length is not None and content_length > max_bytes:
                raise RuntimeError(
                    f"飞书附件大小 {content_length} 字节，超过读取上限 {max_bytes} 字节"
                )
            chunks: list[bytes] = []
            downloaded = 0
            async for chunk in response.aiter_bytes():
                downloaded += len(chunk)
                if downloaded > max_bytes:
                    raise RuntimeError(
                        f"飞书附件超过读取上限 {max_bytes} 字节"
                    )
                chunks.append(chunk)
            return DownloadedFile(
                content=b"".join(chunks),
                content_type=response.headers.get("content-type", "")
                .split(";", 1)[0]
                .strip()
                .lower(),
                filename=_content_disposition_filename(
                    response.headers.get("content-disposition", "")
                ),
            )

    async def _download_tenant_binary(
        self,
        path: str,
        *,
        max_bytes: int,
    ) -> DownloadedFile:
        token = await self._tenant_access_token()
        url = f"{self.settings.feishu_base_url.rstrip('/')}{path}"
        async with (
            httpx.AsyncClient(
                timeout=30,
                proxy=self.settings.feishu_http_proxy,
            ) as client,
            client.stream(
                "GET",
                url,
                headers={"Authorization": f"Bearer {token}"},
            ) as response,
        ):
            if response.is_error:
                body = await response.aread()
                detail = _binary_error_detail(body)
                raise RuntimeError(
                    f"Feishu API error HTTP {response.status_code}: {detail}"
                )
            content_length = _int_or_none(response.headers.get("content-length"))
            if content_length is not None and content_length > max_bytes:
                raise RuntimeError(
                    f"飞书消息资源大小 {content_length} 字节，超过读取上限 {max_bytes} 字节"
                )
            chunks: list[bytes] = []
            downloaded = 0
            async for chunk in response.aiter_bytes():
                downloaded += len(chunk)
                if downloaded > max_bytes:
                    raise RuntimeError(f"飞书消息资源超过读取上限 {max_bytes} 字节")
                chunks.append(chunk)
            return DownloadedFile(
                content=b"".join(chunks),
                content_type=response.headers.get("content-type", "")
                .split(";", 1)[0]
                .strip()
                .lower(),
                filename=_content_disposition_filename(
                    response.headers.get("content-disposition", "")
                ),
            )

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

    async def require_user_authorization(
        self,
        actor_id: str,
        *,
        required_scope_groups: tuple[tuple[str, ...], ...] | None = None,
    ) -> None:
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
        if not access_token:
            raise RuntimeError("请先在飞书中发送 /授权 完成授权后再读取聊天上下文")

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
        async with httpx.AsyncClient(
            timeout=20,
            proxy=self.settings.feishu_http_proxy,
        ) as client:
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


def _binary_error_detail(body: bytes) -> str:
    try:
        data = cast(dict[str, Any], httpx.Response(200, content=body).json())
    except (ValueError, TypeError):
        return body.decode("utf-8", errors="replace")[:500] or "下载失败"
    code = data.get("code")
    message = data.get("msg") or data.get("message") or "下载失败"
    return f"{code}: {message}" if code is not None else str(message)


def _content_disposition_filename(value: str) -> str | None:
    encoded = re.search(r"filename\*=UTF-8''([^;]+)", value, flags=re.IGNORECASE)
    if encoded:
        return unquote(encoded.group(1)).strip().strip('"') or None
    plain = re.search(r'filename="?([^";]+)"?', value, flags=re.IGNORECASE)
    if plain:
        return plain.group(1).strip() or None
    return None


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
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


def _unix_seconds(value: Any) -> int:
    if hasattr(value, "timestamp"):
        return int(value.timestamp())
    return int(value)
