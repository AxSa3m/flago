import pytest
import respx
from httpx import Response

from fcgo.config import Settings
from fcgo.models import ResourceRef, ResourceType
from fcgo.resources.reader import FeishuResourceReader, WebResourceReader


class FakeFeishuAPI:
    def __init__(self) -> None:
        self.doc_calls: list[tuple[str, str]] = []
        self.wiki_calls: list[tuple[str, str]] = []
        self.doc_payload: dict[str, str] = {"title": "测试文档", "content": "文档正文"}
        self.sheet_calls: list[tuple[str, str, str]] = []
        self.sheet_metadata_calls: list[tuple[str, str]] = []
        self.sheet_payload: dict[str, dict[str, object]] = {
            "valueRange": {
                "range": "sheet1!A1:B3",
                "values": [["项目", "状态"], ["A", "完成"], ["B", "进行中"]],
            }
        }
        self.wiki_payload: dict[str, dict[str, str]] = {
            "node": {
                "obj_token": "docx_from_wiki",
                "obj_type": "docx",
                "title": "Wiki 文档",
            }
        }
        self.bitable_table_calls: list[tuple[str, str]] = []
        self.bitable_view_calls: list[tuple[str, str, str]] = []
        self.bitable_field_calls: list[tuple[str, str, str]] = []
        self.bitable_record_calls: list[tuple[str, str, str, int, str | None]] = []
        self.bitable_tables_payload: dict[str, object] = {
            "items": [{"table_id": "tbl1", "name": "关键词表"}]
        }
        self.bitable_views_payload: dict[str, object] = {
            "items": [{"view_id": "vew1", "view_name": "表格"}]
        }
        self.bitable_fields_payload: dict[str, object] = {
            "items": [
                {"field_id": "fld1", "field_name": "关键词", "type": 1},
                {"field_id": "fld2", "field_name": "标题", "type": 1},
                {"field_id": "fld3", "field_name": "日期", "type": 5},
            ]
        }
        self.bitable_records_payload: dict[str, object] = {
            "items": [
                {
                    "record_id": "rec1",
                    "fields": {"关键词": "汽车品牌宣发", "标题": "案例", "日期": "2026-05-28"},
                },
                {"record_id": "rec2", "fields": {"关键词": "数据运营", "标题": ""}},
            ],
            "has_more": False,
        }

    async def get_doc_raw_content(self, document_id: str, actor_id: str) -> dict[str, str]:
        self.doc_calls.append((document_id, actor_id))
        return self.doc_payload

    async def get_wiki_node(self, wiki_token: str, actor_id: str) -> dict[str, dict[str, str]]:
        self.wiki_calls.append((wiki_token, actor_id))
        return self.wiki_payload

    async def read_sheet_range(
        self,
        spreadsheet_token: str,
        range_name: str,
        actor_id: str,
    ) -> dict[str, dict[str, object]]:
        self.sheet_calls.append((spreadsheet_token, range_name, actor_id))
        return self.sheet_payload

    async def query_sheet_metadata(
        self,
        spreadsheet_token: str,
        actor_id: str,
    ) -> dict[str, object]:
        self.sheet_metadata_calls.append((spreadsheet_token, actor_id))
        return {"sheets": [{"sheet_id": "sheet1", "title": "Sheet1", "hidden": False}]}

    async def list_bitable_tables(self, app_token: str, actor_id: str) -> dict[str, object]:
        self.bitable_table_calls.append((app_token, actor_id))
        return self.bitable_tables_payload

    async def list_bitable_views(
        self,
        app_token: str,
        table_id: str,
        actor_id: str,
    ) -> dict[str, object]:
        self.bitable_view_calls.append((app_token, table_id, actor_id))
        return self.bitable_views_payload

    async def list_bitable_fields(
        self,
        app_token: str,
        table_id: str,
        actor_id: str,
    ) -> dict[str, object]:
        self.bitable_field_calls.append((app_token, table_id, actor_id))
        return self.bitable_fields_payload

    async def list_bitable_records(
        self,
        app_token: str,
        table_id: str,
        actor_id: str,
        *,
        limit: int,
        view_id: str | None = None,
    ) -> dict[str, object]:
        self.bitable_record_calls.append((app_token, table_id, actor_id, limit, view_id))
        return self.bitable_records_payload


def _settings(
    *,
    max_resource_chars: int = 120_000,
    max_sheet_rows: int = 200,
    max_sheet_columns: int = 26,
) -> Settings:
    return Settings(
        env="test",
        gemini_api_key="",
        max_resource_chars=max_resource_chars,
        max_sheet_rows=max_sheet_rows,
        max_sheet_columns=max_sheet_columns,
    )


@pytest.mark.asyncio
async def test_read_feishu_doc_content_without_persistence() -> None:
    api = FakeFeishuAPI()
    reader = FeishuResourceReader(_settings(), api)
    ref = ResourceRef(
        type=ResourceType.FEISHU_DOC,
        url="https://docs.feishu.cn/docx/docx123",
        source_kind="docx",
        token="docx123",
    )

    result = await reader.read(ref, "ou_user")

    assert result.title == "测试文档"
    assert result.content == "文档正文"
    assert result.truncated is False
    assert api.doc_calls == [("docx123", "ou_user")]


@pytest.mark.asyncio
async def test_read_feishu_doc_content_is_truncated_by_limit() -> None:
    api = FakeFeishuAPI()
    api.doc_payload = {"content": "1234567890"}
    reader = FeishuResourceReader(_settings(max_resource_chars=5), api)
    ref = ResourceRef(
        type=ResourceType.FEISHU_DOC,
        url="https://docs.feishu.cn/docx/docx123",
        token="docx123",
    )

    result = await reader.read(ref, "ou_user")

    assert result.content == "12345\n\n[内容已截断]"
    assert result.truncated is True


@pytest.mark.asyncio
async def test_read_feishu_wiki_doc_resolves_node_before_reading_content() -> None:
    api = FakeFeishuAPI()
    reader = FeishuResourceReader(_settings(), api)
    ref = ResourceRef(
        type=ResourceType.FEISHU_DOC,
        url="https://docs.feishu.cn/wiki/wiki123",
        source_kind="wiki",
        token="wiki123",
    )

    result = await reader.read(ref, "ou_user")

    assert result.title == "测试文档"
    assert result.content == "文档正文"
    assert api.wiki_calls == [("wiki123", "ou_user")]
    assert api.doc_calls == [("docx_from_wiki", "ou_user")]


@pytest.mark.asyncio
async def test_read_feishu_wiki_rejects_non_doc_nodes() -> None:
    api = FakeFeishuAPI()
    api.wiki_payload = {"node": {"obj_token": "mind123", "obj_type": "mindnote"}}
    reader = FeishuResourceReader(_settings(), api)
    ref = ResourceRef(
        type=ResourceType.FEISHU_DOC,
        url="https://docs.feishu.cn/wiki/wiki123",
        source_kind="wiki",
        token="wiki123",
    )

    result = await reader.read(ref, "ou_user")

    assert result.error == "暂不支持读取该类型的 Wiki 节点：mindnote"
    assert api.doc_calls == []


@pytest.mark.asyncio
async def test_read_feishu_sheet_uses_sheet_id_and_default_safe_range() -> None:
    api = FakeFeishuAPI()
    reader = FeishuResourceReader(_settings(), api)
    ref = ResourceRef(
        type=ResourceType.FEISHU_SHEET,
        url="https://docs.feishu.cn/sheets/sht123?sheet=sheet1",
        source_kind="sheets",
        token="sht123",
        sheet_id="sheet1",
    )

    result = await reader.read(ref, "ou_user")

    assert result.title == "电子表格范围 sheet1!A1:B3"
    assert "读取范围：sheet1!A1:B3" in result.content
    assert "非空单元格数量：6" in result.content
    assert "建议追加起始行：4" in result.content
    assert "| 项目 | 状态 |" in result.content
    assert api.sheet_calls == [("sht123", "sheet1!A1:Z200", "ou_user")]
    assert api.sheet_metadata_calls == []


@pytest.mark.asyncio
async def test_read_feishu_sheet_resolves_first_sheet_id_when_missing() -> None:
    api = FakeFeishuAPI()
    api.sheet_payload = {
        "valueRange": {
            "range": "sheet1!A1:B2",
            "values": [["标题", "内容"], ["A", "B"]],
        }
    }
    reader = FeishuResourceReader(_settings(), api)
    ref = ResourceRef(
        type=ResourceType.FEISHU_SHEET,
        url="https://docs.feishu.cn/sheets/sht123",
        source_kind="sheets",
        token="sht123",
    )

    result = await reader.read(ref, "ou_user")

    assert result.ref.sheet_id == "sheet1"
    assert result.title == "Sheet1范围 sheet1!A1:B2"
    assert "建议追加起始行：3" in result.content
    assert "| 标题 | 内容 |" in result.content
    assert api.sheet_metadata_calls == [("sht123", "ou_user")]
    assert api.sheet_calls == [("sht123", "sheet1!A1:Z200", "ou_user")]


@pytest.mark.asyncio
async def test_read_feishu_sheet_prefixes_explicit_range_with_sheet_id() -> None:
    api = FakeFeishuAPI()
    reader = FeishuResourceReader(_settings(), api)
    ref = ResourceRef(
        type=ResourceType.FEISHU_SHEET,
        url="https://docs.feishu.cn/sheets/sht123?sheet=sheet1&range=A2:C5",
        token="sht123",
        sheet_id="sheet1",
        range_hint="A2:C5",
    )

    result = await reader.read(ref, "ou_user")

    assert result.error is None
    assert api.sheet_calls == [("sht123", "sheet1!A2:C5", "ou_user")]


@pytest.mark.asyncio
async def test_read_feishu_sheet_preserves_range_with_embedded_sheet_id() -> None:
    api = FakeFeishuAPI()
    reader = FeishuResourceReader(_settings(), api)
    ref = ResourceRef(
        type=ResourceType.FEISHU_SHEET,
        url="https://docs.feishu.cn/sheets/sht123?range=sheet2!A1:B2",
        token="sht123",
        sheet_id="sheet1",
        range_hint="sheet2!A1:B2",
    )

    await reader.read(ref, "ou_user")

    assert api.sheet_calls == [("sht123", "sheet2!A1:B2", "ou_user")]


@pytest.mark.asyncio
async def test_read_feishu_sheet_limits_rows_and_columns_with_notice() -> None:
    api = FakeFeishuAPI()
    api.sheet_payload = {
        "valueRange": {
            "range": "sheet1!A1:D3",
            "values": [["A", "B", "C"], ["1", "2", "3"], ["4", "5", "6"]],
        }
    }
    reader = FeishuResourceReader(_settings(max_sheet_rows=2, max_sheet_columns=2), api)
    ref = ResourceRef(type=ResourceType.FEISHU_SHEET, url="https://x/sheets/sht123", token="sht123")

    result = await reader.read(ref, "ou_user")

    assert result.truncated is True
    assert "非空单元格数量：4" in result.content
    assert "建议追加起始行：3" in result.content
    assert "提示：表格已按安全上限截断到前 2 行、2 列。" in result.content
    assert "| A | B |" in result.content
    assert "C" not in result.content
    assert "| 4 | 5 |" not in result.content


@pytest.mark.asyncio
async def test_read_feishu_sheet_trims_empty_outer_rows_and_columns() -> None:
    api = FakeFeishuAPI()
    api.sheet_payload = {
        "valueRange": {
            "range": "sheet1!A1:D4",
            "values": [["", "", "", ""], ["", "名称", "值", ""], ["", "A", "1", ""]],
        }
    }
    reader = FeishuResourceReader(_settings(), api)
    ref = ResourceRef(
        type=ResourceType.FEISHU_SHEET,
        url="https://docs.feishu.cn/sheets/sht123?sheet=sheet1",
        token="sht123",
        sheet_id="sheet1",
    )

    result = await reader.read(ref, "ou_user")

    assert "| 名称 | 值 |" in result.content
    assert "建议追加起始行：4" in result.content
    assert "周边空白不代表表格为空" in result.content
    assert "- 第 1 行：名称 | 值" in result.content
    assert "|  | 名称 | 值 |  |" not in result.content


@pytest.mark.asyncio
async def test_read_feishu_bitable_uses_table_view_fields_and_records() -> None:
    api = FakeFeishuAPI()
    reader = FeishuResourceReader(_settings(), api)
    ref = ResourceRef(
        type=ResourceType.FEISHU_BITABLE,
        url="https://docs.feishu.cn/base/app123?table=tbl1&view=vew1",
        source_kind="base",
        token="app123",
        table_id="tbl1",
        view_id="vew1",
    )

    result = await reader.read(ref, "ou_user")

    assert result.error is None
    assert result.title == "多维表格 tbl1"
    assert "数据表：tbl1" in result.content
    assert "视图：表格" in result.content
    assert "字段：" in result.content
    assert "| 关键词 | 标题 | 日期 |" in result.content
    assert "记录索引：" in result.content
    assert "record_id=rec1" in result.content
    assert "汽车品牌宣发" in result.content
    assert api.bitable_table_calls == []
    assert api.bitable_view_calls == [("app123", "tbl1", "ou_user")]
    assert api.bitable_field_calls == [("app123", "tbl1", "ou_user")]
    assert api.bitable_record_calls == [("app123", "tbl1", "ou_user", 200, "vew1")]


@pytest.mark.asyncio
async def test_read_feishu_bitable_handles_base_v3_matrix_records_and_skips_empty_rows() -> None:
    api = FakeFeishuAPI()
    api.bitable_records_payload = {
        "fields": ["关键词", "标题", "日期", "附件"],
        "field_id_list": ["fld1", "fld2", "fld3", "fld4"],
        "record_id_list": ["rec1", "rec2", "rec3", "rec4", "rec5", "rec6"],
        "data": [
            ["汽车品牌宣发", None, None, None],
            ["品牌设计咨询", None, None, None],
            ["数据运营", None, None, None],
            ["内容运营", None, None, None],
            ["KOL渠道运营", None, None, None],
            [None, None, None, None],
        ],
        "has_more": False,
    }
    reader = FeishuResourceReader(_settings(), api)
    ref = ResourceRef(
        type=ResourceType.FEISHU_BITABLE,
        url="https://docs.feishu.cn/base/app123?table=tbl1",
        source_kind="base",
        token="app123",
        table_id="tbl1",
    )

    result = await reader.read(ref, "ou_user")

    assert "读取记录数：5" in result.content
    assert "| 关键词 | 标题 | 日期 |" in result.content
    assert "汽车品牌宣发" in result.content
    assert "KOL渠道运营" in result.content
    assert "读取记录数：6" not in result.content


@pytest.mark.asyncio
async def test_read_feishu_bitable_preserves_matrix_headers_when_records_are_empty() -> None:
    api = FakeFeishuAPI()
    api.bitable_fields_payload = {"items": []}
    api.bitable_records_payload = {
        "fields": ["文本"],
        "field_id_list": ["fld_text"],
        "data": [[None], [None]],
        "has_more": False,
    }
    reader = FeishuResourceReader(_settings(), api)
    ref = ResourceRef(
        type=ResourceType.FEISHU_BITABLE,
        url="https://docs.feishu.cn/base/app123?table=tbl1",
        source_kind="base",
        token="app123",
        table_id="tbl1",
    )

    result = await reader.read(ref, "ou_user")

    assert "字段：" in result.content
    assert "- 文本" in result.content
    assert "读取记录数：0" in result.content


@pytest.mark.asyncio
async def test_read_feishu_bitable_falls_back_when_view_records_fail() -> None:
    class ViewFailingAPI(FakeFeishuAPI):
        async def list_bitable_records(
            self,
            app_token: str,
            table_id: str,
            actor_id: str,
            *,
            limit: int,
            view_id: str | None = None,
        ) -> dict[str, object]:
            self.bitable_record_calls.append((app_token, table_id, actor_id, limit, view_id))
            if view_id:
                raise RuntimeError("Feishu API error 800030501: not_found")
            return {
                "fields": ["测试内容"],
                "field_id_list": ["fld_text"],
                "data": [[None], [None]],
                "has_more": False,
            }

    api = ViewFailingAPI()
    api.bitable_fields_payload = {"items": []}
    reader = FeishuResourceReader(_settings(), api)
    ref = ResourceRef(
        type=ResourceType.FEISHU_BITABLE,
        url="https://docs.feishu.cn/base/app123?table=tbl1&view=vew_bad",
        source_kind="base",
        token="app123",
        table_id="tbl1",
        view_id="vew_bad",
    )

    result = await reader.read(ref, "ou_user")

    assert result.error is None
    assert "- 测试内容" in result.content
    assert "指定视图读取失败，已回退到默认记录范围" in result.content
    assert api.bitable_record_calls == [
        ("app123", "tbl1", "ou_user", 200, "vew_bad"),
        ("app123", "tbl1", "ou_user", 200, None),
    ]


@pytest.mark.asyncio
async def test_read_feishu_bitable_resolves_first_table_when_missing() -> None:
    api = FakeFeishuAPI()
    reader = FeishuResourceReader(_settings(), api)
    ref = ResourceRef(
        type=ResourceType.FEISHU_BITABLE,
        url="https://docs.feishu.cn/base/app123",
        source_kind="base",
        token="app123",
    )

    result = await reader.read(ref, "ou_user")

    assert result.error is None
    assert result.ref.table_id == "tbl1"
    assert result.title == "关键词表"
    assert "数据表：关键词表" in result.content
    assert api.bitable_table_calls == [("app123", "ou_user")]
    assert api.bitable_record_calls == [("app123", "tbl1", "ou_user", 200, None)]


@pytest.mark.asyncio
async def test_read_feishu_bitable_preserves_fields_when_record_read_fails() -> None:
    class RecordFailingAPI(FakeFeishuAPI):
        async def list_bitable_records(
            self,
            app_token: str,
            table_id: str,
            actor_id: str,
            *,
            limit: int,
            view_id: str | None = None,
        ) -> dict[str, object]:
            self.bitable_record_calls.append((app_token, table_id, actor_id, limit, view_id))
            raise RuntimeError("Feishu API error 5000:")

    api = RecordFailingAPI()
    reader = FeishuResourceReader(_settings(), api)
    ref = ResourceRef(
        type=ResourceType.FEISHU_BITABLE,
        url="https://docs.feishu.cn/base/app123?table=tbl1",
        source_kind="base",
        token="app123",
        table_id="tbl1",
    )

    result = await reader.read(ref, "ou_user")

    assert result.error is None
    assert "字段：" in result.content
    assert "- 关键词" in result.content
    assert "记录读取失败：Feishu API error 5000:" in result.content
    assert "记录摘录：\n[没有读取到记录]" in result.content


@pytest.mark.asyncio
async def test_read_feishu_wiki_bitable_resolves_node_before_reading_records() -> None:
    api = FakeFeishuAPI()
    api.wiki_payload = {
        "node": {"obj_token": "app_from_wiki", "obj_type": "bitable", "title": "Wiki Base"}
    }
    reader = FeishuResourceReader(_settings(), api)
    ref = ResourceRef(
        type=ResourceType.FEISHU_DOC,
        url="https://docs.feishu.cn/wiki/wiki123",
        source_kind="wiki",
        token="wiki123",
    )

    result = await reader.read(ref, "ou_user")

    assert result.error is None
    assert result.title == "Wiki Base - 关键词表"
    assert result.ref.type == ResourceType.FEISHU_BITABLE
    assert result.ref.table_id == "tbl1"
    assert api.wiki_calls == [("wiki123", "ou_user")]
    assert api.bitable_table_calls == [("app_from_wiki", "ou_user")]
    assert api.bitable_record_calls == [("app_from_wiki", "tbl1", "ou_user", 200, None)]


@pytest.mark.asyncio
async def test_read_feishu_bitable_reports_empty_table_list() -> None:
    api = FakeFeishuAPI()
    api.bitable_tables_payload = {"items": []}
    reader = FeishuResourceReader(_settings(), api)
    ref = ResourceRef(
        type=ResourceType.FEISHU_BITABLE,
        url="https://docs.feishu.cn/base/app123",
        token="app123",
    )

    result = await reader.read(ref, "ou_user")

    assert result.error == "多维表格没有可读取的数据表"
    assert api.bitable_record_calls == []


@pytest.mark.asyncio
@respx.mock
async def test_read_web_resource_extracts_readable_text() -> None:
    respx.get("https://example.com/article").mock(
        return_value=Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            text="""
            <html>
              <head><title>测试文章</title><style>.hidden{}</style></head>
              <body>
                <nav>导航</nav>
                <h1>标题</h1>
                <script>console.log("skip")</script>
                <p>第一段正文。</p>
                <p>第二段正文。</p>
              </body>
            </html>
            """,
        )
    )
    reader = WebResourceReader(_settings(max_resource_chars=120_000))
    ref = ResourceRef(type=ResourceType.WEB, url="https://example.com/article")

    result = await reader.read(ref, "ou_user")

    assert result.error is None
    assert result.title == "测试文章"
    assert "标题" in result.content
    assert "第一段正文。" in result.content
    assert "console.log" not in result.content


@pytest.mark.asyncio
@respx.mock
async def test_read_web_resource_rejects_binary_content_type() -> None:
    respx.get("https://example.com/file.pdf").mock(
        return_value=Response(
            200,
            headers={"content-type": "application/pdf"},
            content=b"%PDF",
        )
    )
    reader = WebResourceReader(_settings())
    ref = ResourceRef(type=ResourceType.WEB, url="https://example.com/file.pdf")

    result = await reader.read(ref, "ou_user")

    assert result.error == "不支持读取该网页内容类型：application/pdf"


@pytest.mark.asyncio
@respx.mock
async def test_read_web_resource_truncates_text_by_limit() -> None:
    respx.get("https://example.com/long").mock(
        return_value=Response(
            200,
            headers={"content-type": "text/plain"},
            text="1234567890",
        )
    )
    reader = WebResourceReader(_settings(max_resource_chars=5))
    ref = ResourceRef(type=ResourceType.WEB, url="https://example.com/long")

    result = await reader.read(ref, "ou_user")

    assert result.truncated is True
    assert result.content == "12345\n\n[内容已截断]"
