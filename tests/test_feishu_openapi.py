from datetime import UTC, datetime, timedelta

import pytest
import respx
from httpx import Response

from fcgo.config import Settings
from fcgo.feishu.openapi import FeishuOpenAPI
from fcgo.storage import SQLiteStore


def _settings(tmp_path) -> Settings:
    return Settings(
        env="test",
        sqlite_path=tmp_path / "fcgo.sqlite3",
        feishu_app_id="cli_test",
        feishu_app_secret="secret",
        feishu_base_url="https://open.feishu.test",
        gemini_api_key="",
    )


@pytest.mark.asyncio
async def test_doc_read_requires_user_oauth_token(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "fcgo.sqlite3")
    await store.init()
    api = FeishuOpenAPI(_settings(tmp_path), store)

    with pytest.raises(RuntimeError, match="/授权"):
        await api.get_doc_raw_content("docx123", "ou_user")


@pytest.mark.asyncio
@respx.mock
async def test_doc_read_uses_user_oauth_token(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "fcgo.sqlite3")
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
    store = SQLiteStore(tmp_path / "fcgo.sqlite3")
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
async def test_http_error_includes_feishu_error_body(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "fcgo.sqlite3")
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
async def test_sheet_read_uses_user_token_and_render_options(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "fcgo.sqlite3")
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
    store = SQLiteStore(tmp_path / "fcgo.sqlite3")
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
async def test_bitable_table_list_uses_base_v3_and_user_token(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "fcgo.sqlite3")
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
    route = respx.get("https://open.feishu.test/open-apis/base/v3/bases/app123/tables").mock(
        return_value=Response(
            200,
            json={"code": 0, "data": {"items": [{"table_id": "tbl1"}]}},
        )
    )

    data = await api.list_bitable_tables("app123", "ou_user", limit=200)

    request = route.calls.last.request
    assert data == {"items": [{"table_id": "tbl1"}]}
    assert request.headers["authorization"] == "Bearer u-access"
    assert request.url.params["limit"] == "200"
    assert request.url.params["offset"] == "0"


@pytest.mark.asyncio
@respx.mock
async def test_bitable_field_and_view_lists_use_base_v3(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "fcgo.sqlite3")
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
        "https://open.feishu.test/open-apis/base/v3/bases/app123/tables/tbl1/fields"
    ).mock(return_value=Response(200, json={"code": 0, "data": {"items": [{"name": "关键词"}]}}))
    views_route = respx.get(
        "https://open.feishu.test/open-apis/base/v3/bases/app123/tables/tbl1/views"
    ).mock(return_value=Response(200, json={"code": 0, "data": {"items": [{"name": "表格"}]}}))

    fields = await api.list_bitable_fields("app123", "tbl1", "ou_user")
    views = await api.list_bitable_views("app123", "tbl1", "ou_user")

    assert fields == {"items": [{"name": "关键词"}]}
    assert views == {"items": [{"name": "表格"}]}
    assert fields_route.calls.last.request.headers["authorization"] == "Bearer u-access"
    assert views_route.calls.last.request.headers["authorization"] == "Bearer u-access"


@pytest.mark.asyncio
@respx.mock
async def test_bitable_record_list_passes_limit_offset_and_view_id(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "fcgo.sqlite3")
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
        "https://open.feishu.test/open-apis/base/v3/bases/app123/tables/tbl1/records"
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
    assert request.url.params["limit"] == "50"
    assert request.url.params["offset"] == "0"
    assert request.url.params["view_id"] == "vew1"


@pytest.mark.asyncio
@respx.mock
async def test_sheet_write_range_requires_user_write_scope(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "fcgo.sqlite3")
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
    store = SQLiteStore(tmp_path / "fcgo.sqlite3")
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
    store = SQLiteStore(tmp_path / "fcgo.sqlite3")
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
async def test_create_bitable_record_requires_user_write_scope(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "fcgo.sqlite3")
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
    store = SQLiteStore(tmp_path / "fcgo.sqlite3")
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
    store = SQLiteStore(tmp_path / "fcgo.sqlite3")
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
