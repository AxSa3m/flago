import json
from datetime import UTC, datetime, timedelta

import aiosqlite
import pytest
import respx
from httpx import Response

from flgo.config import Settings
from flgo.feishu.openapi import FeishuOpenAPI
from flgo.storage import SQLiteStore


def _settings(tmp_path) -> Settings:
    return Settings(
        env="test",
        sqlite_path=tmp_path / "flgo.sqlite3",
        feishu_app_id="cli_test",
        feishu_app_secret="secret",
        feishu_base_url="https://open.feishu.test",
        gemini_api_key="",
    )


@pytest.mark.asyncio
async def test_doc_read_requires_user_oauth_token(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "flgo.sqlite3")
    await store.init()
    api = FeishuOpenAPI(_settings(tmp_path), store)

    with pytest.raises(RuntimeError, match="/授权"):
        await api.get_doc_raw_content("docx123", "ou_user")


@pytest.mark.asyncio
@respx.mock
async def test_doc_read_uses_user_oauth_token(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "flgo.sqlite3")
    await store.init()
    await store.save_oauth_token(
        "ou_user",
        {
            "access_token": "u-access",
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            "scope": "auth:user.id:read docx:document:readonly",
        },
    )
    api = FeishuOpenAPI(_settings(tmp_path), store)
    route = respx.get(
        "https://open.feishu.test/open-apis/docx/v1/documents/docx123/raw_content"
    ).mock(return_value=Response(200, json={"code": 0, "data": {"content": "正文"}}))

    data = await api.get_doc_raw_content("docx123", "ou_user")

    assert data == {"content": "正文"}
    assert route.calls.last.request.headers["authorization"] == "Bearer u-access"


@pytest.mark.asyncio
async def test_doc_read_reports_missing_oauth_scope(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "flgo.sqlite3")
    await store.init()
    await store.save_oauth_token(
        "ou_user",
        {
            "access_token": "u-access",
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            "scope": "auth:user.id:read",
        },
    )
    api = FeishuOpenAPI(_settings(tmp_path), store)

    with pytest.raises(RuntimeError, match="docx:document:readonly"):
        await api.get_doc_raw_content("docx123", "ou_user")


@pytest.mark.asyncio
@respx.mock
async def test_list_doc_blocks_paginates_with_user_token(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "flgo.sqlite3")
    await store.init()
    await store.save_oauth_token(
        "ou_user",
        {
            "access_token": "u-access",
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            "scope": "auth:user.id:read docx:document:readonly",
        },
    )
    api = FeishuOpenAPI(_settings(tmp_path), store)
    route = respx.get(
        "https://open.feishu.test/open-apis/docx/v1/documents/docx123/blocks"
    ).mock(
        side_effect=[
            Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "items": [{"block_id": "block-1", "block_type": 2}],
                        "has_more": True,
                        "page_token": "next-page",
                    },
                },
            ),
            Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "items": [
                            {
                                "block_id": "block-2",
                                "block_type": 23,
                                "file": {"token": "file-1", "name": "附件.pdf"},
                            }
                        ],
                        "has_more": False,
                    },
                },
            ),
        ]
    )

    blocks = await api.list_doc_blocks("docx123", "ou_user", max_items=10)

    assert [block["block_id"] for block in blocks] == ["block-1", "block-2"]
    assert len(route.calls) == 2
    assert route.calls[1].request.url.params["page_token"] == "next-page"
    assert route.calls[0].request.headers["authorization"] == "Bearer u-access"


@pytest.mark.asyncio
@respx.mock
async def test_download_doc_media_returns_binary_metadata(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "flgo.sqlite3")
    await store.init()
    await store.save_oauth_token(
        "ou_user",
        {
            "access_token": "u-access",
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            "scope": "auth:user.id:read docs:document.media:download",
        },
    )
    api = FeishuOpenAPI(_settings(tmp_path), store)
    route = respx.get(
        "https://open.feishu.test/open-apis/drive/v1/medias/file-1/download"
    ).mock(
        return_value=Response(
            200,
            headers={
                "content-type": "application/pdf",
                "content-disposition": "attachment; filename*=UTF-8''%E6%B5%8B%E8%AF%95.pdf",
            },
            content=b"%PDF-test",
        )
    )

    downloaded = await api.download_doc_media(
        "file-1",
        "ou_user",
        max_bytes=1024,
    )

    assert downloaded.content == b"%PDF-test"
    assert downloaded.content_type == "application/pdf"
    assert downloaded.filename == "测试.pdf"
    assert route.calls.last.request.headers["authorization"] == "Bearer u-access"


@pytest.mark.asyncio
async def test_download_doc_media_reports_missing_scope(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "flgo.sqlite3")
    await store.init()
    await store.save_oauth_token(
        "ou_user",
        {
            "access_token": "u-access",
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            "scope": "auth:user.id:read docx:document:readonly",
        },
    )
    api = FeishuOpenAPI(_settings(tmp_path), store)

    with pytest.raises(RuntimeError, match="docs:document.media:download"):
        await api.download_doc_media("file-1", "ou_user", max_bytes=1024)


@pytest.mark.asyncio
@respx.mock
async def test_download_doc_media_rejects_oversized_file(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "flgo.sqlite3")
    await store.init()
    await store.save_oauth_token(
        "ou_user",
        {
            "access_token": "u-access",
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            "scope": "auth:user.id:read docs:document.media:download",
        },
    )
    api = FeishuOpenAPI(_settings(tmp_path), store)
    respx.get(
        "https://open.feishu.test/open-apis/drive/v1/medias/file-1/download"
    ).mock(
        return_value=Response(
            200,
            headers={"content-type": "application/pdf", "content-length": "2048"},
            content=b"x",
        )
    )

    with pytest.raises(RuntimeError, match="超过读取上限"):
        await api.download_doc_media("file-1", "ou_user", max_bytes=1024)


@pytest.mark.asyncio
@respx.mock
async def test_search_docs_uses_user_token_and_audits_without_query_body(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "flgo.sqlite3")
    await store.init()
    await store.save_oauth_token(
        "ou_user",
        {
            "access_token": "u-access",
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            "scope": "auth:user.id:read search:docs:read",
        },
    )
    api = FeishuOpenAPI(_settings(tmp_path), store)
    route = respx.post(
        "https://open.feishu.test/open-apis/suite/docs-api/search/object"
    ).mock(
        return_value=Response(
            200,
            json={
                "code": 0,
                "data": {
                    "docs_entities": [
                        {
                            "docs_token": "docx123",
                            "docs_type": "doc",
                            "title": "项目计划",
                        }
                    ],
                    "has_more": False,
                    "total": 1,
                },
            },
        )
    )

    data = await api.search_docs("项目计划", "ou_user", count=5)

    request = route.calls.last.request
    request_body = json.loads(request.content)
    assert data["docs_entities"][0]["docs_token"] == "docx123"
    assert request.headers["authorization"] == "Bearer u-access"
    assert request_body["search_key"] == "项目计划"
    assert request_body["count"] == 5
    async with aiosqlite.connect(store.path) as db:
        rows = await db.execute_fetchall(
            "SELECT detail_json FROM audit_events WHERE event_type = 'resource_searched'"
        )
    assert '"query_length":4' in rows[0][0].replace(" ", "")
    assert "项目计划" not in rows[0][0]


@pytest.mark.asyncio
async def test_search_docs_requires_user_oauth_scope(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "flgo.sqlite3")
    await store.init()
    await store.save_oauth_token(
        "ou_user",
        {
            "access_token": "u-access",
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            "scope": "auth:user.id:read",
        },
    )
    api = FeishuOpenAPI(_settings(tmp_path), store)

    with pytest.raises(RuntimeError, match="drive:drive.search:readonly"):
        await api.search_docs("项目计划", "ou_user", count=5)


@pytest.mark.asyncio
@respx.mock
async def test_search_docs_accepts_drive_search_scope(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "flgo.sqlite3")
    await store.init()
    await store.save_oauth_token(
        "ou_user",
        {
            "access_token": "u-access",
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            "scope": "auth:user.id:read drive:drive.search:readonly",
        },
    )
    api = FeishuOpenAPI(_settings(tmp_path), store)
    respx.post("https://open.feishu.test/open-apis/suite/docs-api/search/object").mock(
        return_value=Response(
            200,
            json={"code": 0, "data": {"docs_entities": [], "has_more": False, "total": 0}},
        )
    )

    data = await api.search_docs("项目计划", "ou_user", count=5)

    assert data["docs_entities"] == []


@pytest.mark.asyncio
@respx.mock
async def test_http_error_includes_feishu_error_body(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "flgo.sqlite3")
    await store.init()
    await store.save_oauth_token(
        "ou_user",
        {
            "access_token": "u-access",
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            "scope": "auth:user.id:read docx:document:readonly",
        },
    )
    api = FeishuOpenAPI(_settings(tmp_path), store)
    respx.get("https://open.feishu.test/open-apis/docx/v1/documents/bad/raw_content").mock(
        return_value=Response(400, json={"code": 1770001, "msg": "bad document token"})
    )

    with pytest.raises(RuntimeError, match="1770001: bad document token"):
        await api.get_doc_raw_content("bad", "ou_user")


@pytest.mark.asyncio
@respx.mock
async def test_download_message_resource_uses_tenant_token(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "flgo.sqlite3")
    await store.init()
    api = FeishuOpenAPI(_settings(tmp_path), store)
    respx.post("https://open.feishu.test/open-apis/auth/v3/tenant_access_token/internal").mock(
        return_value=Response(
            200,
            json={"code": 0, "tenant_access_token": "tenant-token", "expire": 7200},
        )
    )
    route = respx.get(
        "https://open.feishu.test/open-apis/im/v1/messages/om_1/resources/img_1"
    ).mock(
        return_value=Response(
            200,
            headers={"content-type": "image/png", "content-disposition": 'filename="img.png"'},
            content=b"image",
        )
    )

    result = await api.download_message_resource(
        "om_1",
        "img_1",
        resource_type="image",
        max_bytes=1024,
    )

    assert result.content == b"image"
    assert result.content_type == "image/png"
    assert result.filename == "img.png"
    request = route.calls.last.request
    assert request.headers["authorization"] == "Bearer tenant-token"
    assert request.url.params["type"] == "image"


@pytest.mark.asyncio
@respx.mock
async def test_sheet_read_uses_user_token_and_render_options(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "flgo.sqlite3")
    await store.init()
    await store.save_oauth_token(
        "ou_user",
        {
            "access_token": "u-access",
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            "scope": "auth:user.id:read sheets:spreadsheet:readonly",
        },
    )
    api = FeishuOpenAPI(_settings(tmp_path), store)
    route = respx.get(
        "https://open.feishu.test/open-apis/sheets/v2/spreadsheets/sht123/values/sheet1%21A1%3AB2"
    ).mock(
        return_value=Response(
            200,
            json={"code": 0, "data": {"valueRange": {"values": [["A"]]}}},
        )
    )

    data = await api.read_sheet_range("sht123", "sheet1!A1:B2", "ou_user")

    request = route.calls.last.request
    assert data == {"valueRange": {"values": [["A"]]}}
    assert request.headers["authorization"] == "Bearer u-access"
    assert request.url.params["valueRenderOption"] == "ToString"
    assert request.url.params["dateTimeRenderOption"] == "FormattedString"


@pytest.mark.asyncio
@respx.mock
async def test_sheet_metadata_query_uses_user_token(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "flgo.sqlite3")
    await store.init()
    await store.save_oauth_token(
        "ou_user",
        {
            "access_token": "u-access",
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            "scope": "auth:user.id:read sheets:spreadsheet:readonly",
        },
    )
    api = FeishuOpenAPI(_settings(tmp_path), store)
    route = respx.get(
        "https://open.feishu.test/open-apis/sheets/v3/spreadsheets/sht123/sheets/query"
    ).mock(
        return_value=Response(
            200,
            json={"code": 0, "data": {"sheets": [{"sheet_id": "sheet1"}]}},
        )
    )

    data = await api.query_sheet_metadata("sht123", "ou_user")

    assert data == {"sheets": [{"sheet_id": "sheet1"}]}
    assert route.calls.last.request.headers["authorization"] == "Bearer u-access"


@pytest.mark.asyncio
@respx.mock
async def test_bitable_table_list_uses_bitable_v1_and_user_token(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "flgo.sqlite3")
    await store.init()
    await store.save_oauth_token(
        "ou_user",
        {
            "access_token": "u-access",
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            "scope": "auth:user.id:read bitable:app:readonly base:table:read",
        },
    )
    api = FeishuOpenAPI(_settings(tmp_path), store)
    route = respx.get(
        "https://open.feishu.test/open-apis/bitable/v1/apps/app123/tables"
    ).mock(
        return_value=Response(
            200,
            json={"code": 0, "data": {"items": [{"table_id": "tbl1"}]}},
        )
    )

    data = await api.list_bitable_tables("app123", "ou_user", limit=200)

    request = route.calls.last.request
    assert data == {"items": [{"table_id": "tbl1"}]}
    assert request.headers["authorization"] == "Bearer u-access"
    assert request.url.params["page_size"] == "100"


@pytest.mark.asyncio
@respx.mock
async def test_bitable_table_list_accepts_bitable_readonly_scope(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "flgo.sqlite3")
    await store.init()
    await store.save_oauth_token(
        "ou_user",
        {
            "access_token": "u-access",
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            "scope": "auth:user.id:read bitable:app:readonly",
        },
    )
    api = FeishuOpenAPI(_settings(tmp_path), store)
    route = respx.get(
        "https://open.feishu.test/open-apis/bitable/v1/apps/app123/tables"
    ).mock(return_value=Response(200, json={"code": 0, "data": {"items": []}}))

    data = await api.list_bitable_tables("app123", "ou_user", limit=200)

    assert data == {"items": []}
    assert route.called


@pytest.mark.asyncio
@respx.mock
async def test_bitable_field_and_view_lists_use_bitable_v1(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "flgo.sqlite3")
    await store.init()
    await store.save_oauth_token(
        "ou_user",
        {
            "access_token": "u-access",
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            "scope": (
                "auth:user.id:read bitable:app:readonly base:field:read base:view:read"
            ),
        },
    )
    api = FeishuOpenAPI(_settings(tmp_path), store)
    fields_route = respx.get(
        "https://open.feishu.test/open-apis/bitable/v1/apps/app123/tables/tbl1/fields"
    ).mock(return_value=Response(200, json={"code": 0, "data": {"items": [{"name": "关键词"}]}}))
    views_route = respx.get(
        "https://open.feishu.test/open-apis/bitable/v1/apps/app123/tables/tbl1/views"
    ).mock(return_value=Response(200, json={"code": 0, "data": {"items": [{"name": "表格"}]}}))

    fields = await api.list_bitable_fields("app123", "tbl1", "ou_user")
    views = await api.list_bitable_views("app123", "tbl1", "ou_user")

    assert fields == {"items": [{"name": "关键词"}]}
    assert views == {"items": [{"name": "表格"}]}
    assert fields_route.calls.last.request.headers["authorization"] == "Bearer u-access"
    assert views_route.calls.last.request.headers["authorization"] == "Bearer u-access"


@pytest.mark.asyncio
@respx.mock
async def test_bitable_record_list_passes_page_size_and_view_id(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "flgo.sqlite3")
    await store.init()
    await store.save_oauth_token(
        "ou_user",
        {
            "access_token": "u-access",
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            "scope": "auth:user.id:read bitable:app:readonly base:record:read",
        },
    )
    api = FeishuOpenAPI(_settings(tmp_path), store)
    route = respx.get(
        "https://open.feishu.test/open-apis/bitable/v1/apps/app123/tables/tbl1/records"
    ).mock(
        return_value=Response(
            200,
            json={"code": 0, "data": {"items": [{"fields": {"关键词": "A"}}]}},
        )
    )

    data = await api.list_bitable_records(
        "app123",
        "tbl1",
        "ou_user",
        limit=50,
        view_id="vew1",
    )

    request = route.calls.last.request
    assert data == {"items": [{"fields": {"关键词": "A"}}]}
    assert request.headers["authorization"] == "Bearer u-access"
    assert request.url.params["page_size"] == "50"
    assert request.url.params["view_id"] == "vew1"


@pytest.mark.asyncio
@respx.mock
async def test_list_recent_messages_uses_tenant_token_without_user_oauth(
    tmp_path,
) -> None:
    store = SQLiteStore(tmp_path / "flgo.sqlite3")
    await store.init()
    settings = _settings(tmp_path)
    api = FeishuOpenAPI(settings, store)
    token_route = respx.post(
        "https://open.feishu.test/open-apis/auth/v3/tenant_access_token/internal"
    ).mock(
        return_value=Response(
            200,
            json={"code": 0, "tenant_access_token": "tenant-token", "expire": 7200},
        )
    )
    messages_route = respx.get("https://open.feishu.test/open-apis/im/v1/messages").mock(
        return_value=Response(200, json={"code": 0, "data": {"items": []}})
    )

    data = await api.list_recent_messages(
        actor_id="ou_user",
        container_id_type="chat",
        container_id="oc_chat",
        start_time=datetime(2026, 6, 10, tzinfo=UTC),
        end_time=datetime(2026, 6, 11, tzinfo=UTC),
        page_size=50,
    )

    assert data == {"items": []}
    assert token_route.called
    request = messages_route.calls.last.request
    assert request.headers["authorization"] == "Bearer tenant-token"
    assert request.url.params["container_id_type"] == "chat"
    assert request.url.params["container_id"] == "oc_chat"
    assert request.url.params["page_size"] == "50"


@pytest.mark.asyncio
@respx.mock
async def test_sheet_write_range_requires_user_write_scope(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "flgo.sqlite3")
    await store.init()
    await store.save_oauth_token(
        "ou_user",
        {
            "access_token": "u-access",
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            "scope": "auth:user.id:read sheets:spreadsheet",
        },
    )
    api = FeishuOpenAPI(_settings(tmp_path), store)
    route = respx.put("https://open.feishu.test/open-apis/sheets/v2/spreadsheets/sht123/values").mock(
        return_value=Response(200, json={"code": 0, "data": {"updatedCells": 1}})
    )

    data = await api.write_sheet_range("sht123", "Sheet1!A1:B1", [["A", "B"]], "ou_user")

    assert data == {"updatedCells": 1}
    request = route.calls.last.request
    assert request.headers["authorization"] == "Bearer u-access"
    assert request.content


@pytest.mark.asyncio
@respx.mock
async def test_create_doc_uses_user_write_scope_and_folder_token(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "flgo.sqlite3")
    await store.init()
    await store.save_oauth_token(
        "ou_user",
        {
            "access_token": "u-access",
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            "scope": "auth:user.id:read docx:document",
        },
    )
    api = FeishuOpenAPI(_settings(tmp_path), store)
    route = respx.post("https://open.feishu.test/open-apis/docx/v1/documents").mock(
        return_value=Response(
            200,
            json={"code": 0, "data": {"document_id": "docx123"}},
        )
    )

    data = await api.create_doc("新文档", "ou_user", folder_token="fld123")

    request = route.calls.last.request
    assert data == {"document_id": "docx123"}
    assert request.headers["authorization"] == "Bearer u-access"
    assert request.url.params["folder_token"] == "fld123"
    assert request.content


@pytest.mark.asyncio
@respx.mock
async def test_append_doc_text_converts_to_children_blocks(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "flgo.sqlite3")
    await store.init()
    await store.save_oauth_token(
        "ou_user",
        {
            "access_token": "u-access",
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            "scope": "auth:user.id:read docx:document",
        },
    )
    api = FeishuOpenAPI(_settings(tmp_path), store)
    route = respx.post(
        "https://open.feishu.test/open-apis/docx/v1/documents/docx123/blocks/docx123/children"
    ).mock(return_value=Response(200, json={"code": 0, "data": {"revision_id": 2}}))

    data = await api.append_doc_text("docx123", "第一段\n\n第二段", "ou_user")

    request = route.calls.last.request
    assert data == {"revision_id": 2}
    assert request.url.params["document_revision_id"] == "-1"
    assert request.headers["authorization"] == "Bearer u-access"
    assert b'"index":-1' in request.content
    assert b"children" in request.content


@pytest.mark.asyncio
@respx.mock
async def test_append_doc_text_supports_document_start_index(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "flgo.sqlite3")
    await store.init()
    await store.save_oauth_token(
        "ou_user",
        {
            "access_token": "u-access",
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            "scope": "auth:user.id:read docx:document",
        },
    )
    api = FeishuOpenAPI(_settings(tmp_path), store)
    route = respx.post(
        "https://open.feishu.test/open-apis/docx/v1/documents/docx123/blocks/docx123/children"
    ).mock(return_value=Response(200, json={"code": 0, "data": {"revision_id": 2}}))

    await api.append_doc_text(
        "docx123",
        "开头文字",
        "ou_user",
        block_id="docx123",
        index=0,
    )

    assert b'"index":0' in route.calls.last.request.content


@pytest.mark.asyncio
@respx.mock
async def test_delete_doc_block_uses_user_write_scope(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "flgo.sqlite3")
    await store.init()
    await store.save_oauth_token(
        "ou_user",
        {
            "access_token": "u-access",
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            "scope": "auth:user.id:read docx:document",
        },
    )
    api = FeishuOpenAPI(_settings(tmp_path), store)
    route = respx.put(
        "https://open.feishu.test/open-apis/docs_ai/v1/documents/docx123"
    ).mock(return_value=Response(200, json={"code": 0, "data": {"revision_id": 3}}))

    data = await api.delete_doc_block("docx123", "blk1", "ou_user")

    request = route.calls.last.request
    assert data == {"revision_id": 3}
    assert request.headers["authorization"] == "Bearer u-access"
    assert json.loads(request.content) == {
        "command": "block_delete",
        "block_id": "blk1",
        "format": "xml",
        "revision_id": -1,
    }


@pytest.mark.asyncio
@respx.mock
async def test_create_bitable_record_requires_user_write_scope(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "flgo.sqlite3")
    await store.init()
    await store.save_oauth_token(
        "ou_user",
        {
            "access_token": "u-access",
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            "scope": "auth:user.id:read bitable:app base:record:create",
        },
    )
    api = FeishuOpenAPI(_settings(tmp_path), store)
    route = respx.post(
        "https://open.feishu.test/open-apis/bitable/v1/apps/app123/tables/tbl1/records"
    ).mock(return_value=Response(200, json={"code": 0, "data": {"record": {"record_id": "rec1"}}}))

    data = await api.create_bitable_record("app123", "tbl1", {"关键词": "A"}, "ou_user")

    assert data == {"record": {"record_id": "rec1"}}
    assert route.calls.last.request.headers["authorization"] == "Bearer u-access"


@pytest.mark.asyncio
@respx.mock
async def test_update_bitable_record_requires_user_write_scope(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "flgo.sqlite3")
    await store.init()
    await store.save_oauth_token(
        "ou_user",
        {
            "access_token": "u-access",
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            "scope": "auth:user.id:read bitable:app base:record:update",
        },
    )
    api = FeishuOpenAPI(_settings(tmp_path), store)
    route = respx.put(
        "https://open.feishu.test/open-apis/bitable/v1/apps/app123/tables/tbl1/records/rec1"
    ).mock(return_value=Response(200, json={"code": 0, "data": {"record": {"record_id": "rec1"}}}))

    data = await api.update_bitable_record(
        "app123",
        "tbl1",
        "rec1",
        {"关键词": "B"},
        "ou_user",
    )

    assert data == {"record": {"record_id": "rec1"}}
    assert route.calls.last.request.headers["authorization"] == "Bearer u-access"


@pytest.mark.asyncio
@respx.mock
async def test_delete_bitable_record_requires_user_delete_scope(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "flgo.sqlite3")
    await store.init()
    await store.save_oauth_token(
        "ou_user",
        {
            "access_token": "u-access",
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            "scope": "auth:user.id:read bitable:app base:record:delete",
        },
    )
    api = FeishuOpenAPI(_settings(tmp_path), store)
    route = respx.delete(
        "https://open.feishu.test/open-apis/bitable/v1/apps/app123/tables/tbl1/records/rec1"
    ).mock(return_value=Response(200, json={"code": 0, "data": {"deleted": True}}))

    data = await api.delete_bitable_record("app123", "tbl1", "rec1", "ou_user")

    assert data == {"deleted": True}
    assert route.calls.last.request.headers["authorization"] == "Bearer u-access"
