import json
from io import BytesIO

import aiosqlite
import pytest
import respx
from httpx import AsyncByteStream, Response
from pypdf import PdfWriter

from flago.config import Settings
from flago.feishu.openapi import DownloadedFile
from flago.model_providers.types import ModelRequest, ModelResponse
from flago.models import AuditEventType, ResourceRef, ResourceType
from flago.resources.reader import FeishuResourceReader, FeishuResourceSearcher, WebResourceReader
from flago.storage import SQLiteStore


class FakeFeishuAPI:
    def __init__(self) -> None:
        self.doc_calls: list[tuple[str, str]] = []
        self.doc_block_calls: list[tuple[str, str, int]] = []
        self.doc_blocks_payload: list[dict[str, object]] = []
        self.media_download_calls: list[tuple[str, str, int]] = []
        self.media_payloads: dict[str, DownloadedFile] = {}
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
        self.search_docs_calls: list[tuple[str, str, int]] = []
        self.search_docs_payload: dict[str, object] = {
            "docs_entities": [
                {"docs_token": "docx123", "docs_type": "doc", "title": "测试文档"},
                {"docs_token": "sht123", "docs_type": "sheet", "title": "测试表格"},
                {"docs_token": "app123", "docs_type": "bitable", "title": "测试多维表"},
            ]
        }
        self.search_wiki_calls: list[tuple[str, str, int]] = []
        self.search_wiki_payload: dict[str, object] = {"items": []}

    async def get_doc_raw_content(self, document_id: str, actor_id: str) -> dict[str, str]:
        self.doc_calls.append((document_id, actor_id))
        return self.doc_payload

    async def list_doc_blocks(
        self,
        document_id: str,
        actor_id: str,
        *,
        max_items: int,
    ) -> list[dict[str, object]]:
        self.doc_block_calls.append((document_id, actor_id, max_items))
        return self.doc_blocks_payload[:max_items]

    async def download_doc_media(
        self,
        file_token: str,
        actor_id: str,
        *,
        max_bytes: int,
    ) -> DownloadedFile:
        self.media_download_calls.append((file_token, actor_id, max_bytes))
        return self.media_payloads[file_token]

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

    async def search_docs(
        self,
        query: str,
        actor_id: str,
        *,
        count: int,
    ) -> dict[str, object]:
        self.search_docs_calls.append((query, actor_id, count))
        return self.search_docs_payload

    async def search_wiki_nodes(
        self,
        query: str,
        actor_id: str,
        *,
        page_size: int,
    ) -> dict[str, object]:
        self.search_wiki_calls.append((query, actor_id, page_size))
        return self.search_wiki_payload


class FakeVisionModelRouter:
    def __init__(self, text: str = "图片里写着 Deliverables。") -> None:
        self.text = text
        self.calls: list[ModelRequest] = []

    async def generate_model(self, request: ModelRequest) -> ModelResponse:
        self.calls.append(request)
        return ModelResponse(text=self.text, provider=request.provider, model=request.model)


class TrackingAsyncByteStream(AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.read_count = 0

    async def __aiter__(self):
        for chunk in self.chunks:
            self.read_count += 1
            yield chunk


def _settings(
    *,
    max_resource_chars: int = 120_000,
    max_sheet_rows: int = 200,
    max_sheet_columns: int = 26,
    feishu_docs_base_url: str = "https://docs.feishu.cn",
    embedded_file_limit: int = 3,
    pdf_default_pages: int = 2,
    pdf_max_pages: int = 10,
    attachment_vision_enabled: bool = False,
    attachment_media_understanding_enabled: bool = False,
    web_read_enabled: bool = True,
    web_max_bytes: int = 1_000_000,
    web_allowed_hosts: str = "",
    web_blocked_hosts: str = "",
) -> Settings:
    return Settings(
        env="test",
        gemini_api_key="",
        max_resource_chars=max_resource_chars,
        max_sheet_rows=max_sheet_rows,
        max_sheet_columns=max_sheet_columns,
        feishu_docs_base_url=feishu_docs_base_url,
        embedded_file_limit=embedded_file_limit,
        pdf_default_pages=pdf_default_pages,
        pdf_max_pages=pdf_max_pages,
        attachment_vision_enabled=attachment_vision_enabled,
        attachment_media_understanding_enabled=attachment_media_understanding_enabled,
        web_read_enabled=web_read_enabled,
        web_max_bytes=web_max_bytes,
        web_allowed_hosts=web_allowed_hosts,
        web_blocked_hosts=web_blocked_hosts,
    )


@pytest.mark.asyncio
async def test_search_feishu_resources_uses_configured_docs_base_url() -> None:
    api = FakeFeishuAPI()
    searcher = FeishuResourceSearcher(
        _settings(feishu_docs_base_url="https://my.feishu.cn"),
        api,
    )

    refs = await searcher.search("蓝色火箭", "ou_user", limit=5)

    assert api.search_docs_calls == [("蓝色火箭", "ou_user", 5)]
    assert [ref.url for ref in refs] == [
        "https://my.feishu.cn/docx/docx123",
        "https://my.feishu.cn/sheets/sht123",
        "https://my.feishu.cn/base/app123",
    ]
    assert [ref.title for ref in refs] == ["测试文档", "测试表格", "测试多维表"]


@pytest.mark.asyncio
async def test_search_feishu_resources_prefers_api_returned_url() -> None:
    api = FakeFeishuAPI()
    api.search_docs_payload = {
        "docs_entities": [
            {
                "docs_token": "docx123",
                "docs_type": "doc",
                "title": "测试文档",
                "url": "https://my.feishu.cn/docx/docx123?from=space_home_recent",
            }
        ]
    }
    searcher = FeishuResourceSearcher(_settings(), api)

    refs = await searcher.search("蓝色火箭", "ou_user", limit=5)

    assert refs[0].url == "https://my.feishu.cn/docx/docx123?from=space_home_recent"
    assert refs[0].title == "测试文档"


@pytest.mark.asyncio
async def test_search_feishu_resources_includes_wiki_results() -> None:
    api = FakeFeishuAPI()
    api.search_docs_payload = {"docs_entities": []}
    api.search_wiki_payload = {
        "items": [
            {
                "node_id": "V0pewArU0isGmAkltH3cuhxunwj",
                "obj_token": "docx_real",
                "obj_type": 8,
                "title": "情感调优 无声对白 分镜",
                "url": "https://my.feishu.cn/wiki/V0pewArU0isGmAkltH3cuhxunwj",
            }
        ]
    }
    searcher = FeishuResourceSearcher(_settings(), api)

    refs = await searcher.search("情感调优", "ou_user", limit=5)

    assert api.search_wiki_calls == [("情感调优", "ou_user", 5)]
    assert refs[0].type == ResourceType.FEISHU_DOC
    assert refs[0].url == "https://my.feishu.cn/wiki/V0pewArU0isGmAkltH3cuhxunwj"
    assert refs[0].token == "docx_real"
    assert refs[0].title == "情感调优 无声对白 分镜"
    assert refs[0].source_kind == "search:wiki:feishu_doc"


@pytest.mark.asyncio
async def test_search_feishu_resources_tries_fuzzy_query_variants() -> None:
    api = FakeFeishuAPI()
    api.search_docs_payload = {"docs_entities": []}

    async def search_docs(query: str, actor_id: str, *, count: int) -> dict[str, object]:
        api.search_docs_calls.append((query, actor_id, count))
        if query == "情感调优":
            return {
                "docs_entities": [
                    {"docs_token": "docx456", "docs_type": "doc", "title": "情感调优剧本"}
                ]
            }
        return {"docs_entities": []}

    api.search_docs = search_docs  # type: ignore[method-assign]
    searcher = FeishuResourceSearcher(_settings(), api)

    refs = await searcher.search("情感调优 剧本", "ou_user", limit=5)

    assert [call[0] for call in api.search_docs_calls] == [
        "情感调优 剧本",
        "情感调优剧本",
        "情感调优",
    ]
    assert refs[0].token == "docx456"


@pytest.mark.asyncio
async def test_search_feishu_resources_does_not_fall_back_to_generic_terms() -> None:
    api = FakeFeishuAPI()
    api.search_docs_payload = {"docs_entities": []}
    searcher = FeishuResourceSearcher(_settings(), api)

    refs = await searcher.search("情感调优 剧本", "ou_user", limit=5)

    assert refs == []
    assert [call[0] for call in api.search_docs_calls] == [
        "情感调优 剧本",
        "情感调优剧本",
        "情感调优",
    ]


@pytest.mark.asyncio
async def test_search_feishu_resources_prefers_long_specific_terms() -> None:
    api = FakeFeishuAPI()
    api.search_docs_payload = {"docs_entities": []}

    async def search_wiki_nodes(query: str, actor_id: str, *, page_size: int) -> dict[str, object]:
        api.search_wiki_calls.append((query, actor_id, page_size))
        if query == "情感调优":
            return {
                "items": [
                    {
                        "node_id": "V0pewArU0isGmAkltH3cuhxunwj",
                        "obj_token": "docx_real",
                        "obj_type": 8,
                        "title": "情感调优 无声对白 分镜",
                        "url": "https://my.feishu.cn/wiki/V0pewArU0isGmAkltH3cuhxunwj",
                    }
                ]
            }
        return {"items": []}

    api.search_wiki_nodes = search_wiki_nodes  # type: ignore[method-assign]
    searcher = FeishuResourceSearcher(_settings(), api)

    refs = await searcher.search("一品 情感调优", "ou_user", limit=5)

    assert [call[0] for call in api.search_wiki_calls] == [
        "一品 情感调优",
        "一品情感调优",
        "情感调优",
    ]
    assert refs[0].url == "https://my.feishu.cn/wiki/V0pewArU0isGmAkltH3cuhxunwj"


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
async def test_read_feishu_doc_extracts_default_first_two_pdf_pages() -> None:
    api = FakeFeishuAPI()
    api.doc_blocks_payload = [
        {
            "block_type": 23,
            "file": {"token": "file-pdf", "name": "测试附件.pdf"},
        }
    ]
    api.media_payloads["file-pdf"] = DownloadedFile(
        content=_text_pdf(["First page", "Second page", "Third page"]),
        content_type="application/pdf",
        filename="测试附件.pdf",
    )
    reader = FeishuResourceReader(_settings(), api)
    ref = ResourceRef(
        type=ResourceType.FEISHU_DOC,
        url="https://docs.feishu.cn/docx/docx123",
        token="docx123",
    )

    result = await reader.read(ref, "ou_user")

    assert "[PDF 第 1 页]" in result.content
    assert "First page" in result.content
    assert "[PDF 第 2 页]" in result.content
    assert "Second page" in result.content
    assert "Third page" not in result.content
    assert api.doc_block_calls == [("docx123", "ou_user", 1000)]
    assert api.media_download_calls == [("file-pdf", "ou_user", 20 * 1024 * 1024)]


@pytest.mark.asyncio
async def test_read_feishu_doc_records_embedded_asset_anchor_metadata() -> None:
    api = FakeFeishuAPI()
    api.doc_blocks_payload = [
        {"block_id": "text-1", "parent_id": "docx123", "block_type": 2},
        {
            "block_id": "file-1",
            "parent_id": "docx123",
            "block_type": 23,
            "file": {"token": "file-docx", "name": "Claude Code登录指引.docx"},
        },
    ]
    reader = FeishuResourceReader(_settings(embedded_file_limit=0), api)
    ref = ResourceRef(
        type=ResourceType.FEISHU_DOC,
        url="https://docs.feishu.cn/docx/docx123",
        token="docx123",
    )

    result = await reader.read(ref, "ou_user")

    assert result.metadata["embedded_assets"] == [
        {
            "token": "file-docx",
            "kind": "file",
            "name": "Claude Code登录指引.docx",
            "block_id": "file-1",
            "parent_block_id": "docx123",
            "index": 1,
        }
    ]
    assert api.media_download_calls == []


@pytest.mark.asyncio
async def test_read_feishu_doc_extracts_requested_pdf_page() -> None:
    api = FakeFeishuAPI()
    api.doc_blocks_payload = [
        {
            "block_type": 2,
            "text": {
                "elements": [
                    {
                        "text_run": {"content": "附件"},
                        "file": {
                            "file_token": "inline-pdf",
                            "name": "内联附件.pdf",
                        },
                    }
                ]
            },
        }
    ]
    api.media_payloads["inline-pdf"] = DownloadedFile(
        content=_text_pdf(["Page one", "Page two", "Page three"]),
        content_type="application/pdf",
        filename="内联附件.pdf",
    )
    reader = FeishuResourceReader(_settings(), api)
    ref = ResourceRef(
        type=ResourceType.FEISHU_DOC,
        url="https://docs.feishu.cn/docx/docx123",
        token="docx123",
        range_hint="pdf:page:3",
    )

    result = await reader.read(ref, "ou_user")

    assert "[PDF 第 3 页]" in result.content
    assert "Page three" in result.content
    assert "Page one" not in result.content
    assert "Page two" not in result.content


@pytest.mark.asyncio
async def test_read_feishu_doc_reports_scanned_pdf_without_text() -> None:
    api = FakeFeishuAPI()
    api.doc_blocks_payload = [
        {"block_type": 23, "file": {"token": "scan-pdf", "name": "扫描件.pdf"}}
    ]
    api.media_payloads["scan-pdf"] = DownloadedFile(
        content=_blank_pdf(),
        content_type="application/pdf",
        filename="扫描件.pdf",
    )
    reader = FeishuResourceReader(_settings(), api)
    ref = ResourceRef(
        type=ResourceType.FEISHU_DOC,
        url="https://docs.feishu.cn/docx/docx123",
        token="docx123",
    )

    result = await reader.read(ref, "ou_user")

    assert "可能是扫描版 PDF" in result.content
    assert "暂不支持 OCR" in result.content


@pytest.mark.asyncio
async def test_read_feishu_doc_extracts_text_attachment() -> None:
    api = FakeFeishuAPI()
    api.doc_blocks_payload = [
        {"block_type": 23, "file": {"token": "file-text", "name": "说明.md"}}
    ]
    api.media_payloads["file-text"] = DownloadedFile(
        content="# 目标\n支持多格式附件".encode(),
        content_type="text/markdown",
        filename="说明.md",
    )
    reader = FeishuResourceReader(_settings(), api)
    ref = ResourceRef(
        type=ResourceType.FEISHU_DOC,
        url="https://docs.feishu.cn/docx/docx123",
        token="docx123",
    )

    result = await reader.read(ref, "ou_user")

    assert "### 文本文件：说明.md" in result.content
    assert "支持多格式附件" in result.content
    assert api.media_download_calls == [("file-text", "ou_user", 20 * 1024 * 1024)]


@pytest.mark.asyncio
async def test_read_feishu_doc_extracts_image_metadata_and_document_links() -> None:
    api = FakeFeishuAPI()
    image = BytesIO()
    from PIL import Image

    Image.new("RGB", (64, 48), color="white").save(image, format="PNG")
    api.doc_blocks_payload = [
        {
            "block_type": 27,
            "image": {
                "token": "image-1",
                "caption": {"content": "产品界面截图"},
            },
        },
        {
            "block_type": 2,
            "text": {
                "elements": [
                    {
                        "text_run": {
                            "content": "项目主页",
                            "text_element_style": {
                                "link": {"url": "https%3A%2F%2Fexample.com%2Fproject"}
                            },
                        }
                    },
                    {
                        "mention_doc": {
                            "title": "关联方案",
                            "url": "https%3A%2F%2Fmy.feishu.cn%2Fdocx%2Fdoc123",
                        }
                    },
                ]
            },
        },
        {
            "block_type": 26,
            "iframe": {
                "component": {
                    "url": "https%3A%2F%2Fexample.com%2Fdashboard",
                    "iframe_type": 99,
                }
            },
        },
    ]
    api.media_payloads["image-1"] = DownloadedFile(
        content=image.getvalue(),
        content_type="image/png",
        filename="界面.png",
    )
    reader = FeishuResourceReader(_settings(), api)
    ref = ResourceRef(
        type=ResourceType.FEISHU_DOC,
        url="https://docs.feishu.cn/docx/docx123",
        token="docx123",
    )

    result = await reader.read(ref, "ou_user")

    assert "### 图片：界面.png" in result.content
    assert "尺寸：64 × 48" in result.content
    assert "文档描述：产品界面截图" in result.content
    assert "项目主页 — https://example.com/project" in result.content
    assert "@文档：关联方案 — https://my.feishu.cn/docx/doc123" in result.content
    assert "内嵌网页：https://example.com/dashboard" in result.content


@pytest.mark.asyncio
async def test_read_feishu_doc_describes_image_with_vision_model() -> None:
    api = FakeFeishuAPI()
    image = BytesIO()
    from PIL import Image

    Image.new("RGB", (64, 48), color="white").save(image, format="PNG")
    api.doc_blocks_payload = [
        {
            "block_type": 27,
            "image": {"token": "image-1"},
        }
    ]
    api.media_payloads["image-1"] = DownloadedFile(
        content=image.getvalue(),
        content_type="image/png",
        filename="image.png",
    )
    vision = FakeVisionModelRouter("图片中有英文标题 Deliverables。")
    reader = FeishuResourceReader(
        _settings(attachment_vision_enabled=True),
        api,
        model_router=vision,
    )
    ref = ResourceRef(
        type=ResourceType.FEISHU_DOC,
        url="https://docs.feishu.cn/docx/docx123",
        token="docx123",
    )

    result = await reader.read(ref, "ou_user")

    assert "[视觉模型理解]" in result.content
    assert "Deliverables" in result.content
    assert vision.calls
    assert vision.calls[0].provider == "gemini"
    assert vision.calls[0].messages[0].attachments[0].filename == "image.png"


@pytest.mark.asyncio
async def test_read_feishu_doc_describes_audio_with_multimodal_model() -> None:
    api = FakeFeishuAPI()
    api.doc_blocks_payload = [
        {
            "block_type": 23,
            "file": {"token": "audio-1", "name": "访谈.mp3"},
        }
    ]
    api.media_payloads["audio-1"] = DownloadedFile(
        content=b"ID3 fake audio",
        content_type="audio/mpeg",
        filename="访谈.mp3",
    )
    model = FakeVisionModelRouter("音频中提到项目进度和交付时间。")
    reader = FeishuResourceReader(
        _settings(attachment_media_understanding_enabled=True),
        api,
        model_router=model,
    )
    ref = ResourceRef(
        type=ResourceType.FEISHU_DOC,
        url="https://docs.feishu.cn/docx/docx123",
        token="docx123",
    )

    result = await reader.read(ref, "ou_user")

    assert "[多模态模型理解]" in result.content
    assert "项目进度" in result.content
    assert model.calls
    assert model.calls[0].provider == "gemini"
    assert model.calls[0].messages[0].attachments[0].type == "audio"
    assert model.calls[0].messages[0].attachments[0].media_type == "audio/mpeg"


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
async def test_read_feishu_resource_audits_error_category_without_raw_error(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "flago.sqlite3")
    await store.init()
    api = FakeFeishuAPI()
    api.store = store
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
    audit_details = await _audit_details(store, AuditEventType.RESOURCE_READ.value)
    assert audit_details == [
        {
            "error_length": len("暂不支持读取该类型的 Wiki 节点：mindnote"),
            "error_type": "unsupported_resource",
            "has_error": True,
            "resource_type": "feishu_doc",
            "source_kind": "wiki",
            "truncated": False,
        }
    ]
    assert "mindnote" not in json.dumps(audit_details, ensure_ascii=False)


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
async def test_read_web_resource_can_be_disabled() -> None:
    reader = WebResourceReader(_settings(web_read_enabled=False))
    ref = ResourceRef(type=ResourceType.WEB, url="https://example.com/article")

    result = await reader.read(ref, "ou_user")

    assert result.error == "网页外链读取已关闭"


@pytest.mark.asyncio
async def test_read_web_resource_blocks_private_address() -> None:
    reader = WebResourceReader(_settings())
    ref = ResourceRef(type=ResourceType.WEB, url="http://127.0.0.1:8000/healthz")

    result = await reader.read(ref, "ou_user")

    assert result.error == "为避免访问本机或内网地址，已跳过该网页链接"


@pytest.mark.asyncio
@respx.mock
async def test_read_web_resource_revalidates_redirect_targets() -> None:
    respx.get("https://example.com/redirect").mock(
        return_value=Response(
            302,
            headers={"location": "http://127.0.0.1:8000/healthz"},
        )
    )
    reader = WebResourceReader(_settings())
    ref = ResourceRef(type=ResourceType.WEB, url="https://example.com/redirect")

    result = await reader.read(ref, "ou_user")

    assert result.error == "为避免访问本机或内网地址，已跳过该网页链接"


@pytest.mark.asyncio
async def test_read_web_resource_rejects_url_outside_allowlist() -> None:
    reader = WebResourceReader(_settings(web_allowed_hosts="trusted.example"))
    ref = ResourceRef(type=ResourceType.WEB, url="https://example.com/article")

    result = await reader.read(ref, "ou_user")

    assert result.error == "该网页域名不在允许读取范围内"


@pytest.mark.asyncio
async def test_read_web_resource_rejects_blocked_host() -> None:
    reader = WebResourceReader(_settings(web_blocked_hosts="example.com"))
    ref = ResourceRef(type=ResourceType.WEB, url="https://example.com/article")

    result = await reader.read(ref, "ou_user")

    assert result.error == "该网页域名已被配置为禁止读取"


@pytest.mark.asyncio
@respx.mock
async def test_read_web_resource_rejects_declared_large_content() -> None:
    respx.get("https://example.com/large").mock(
        return_value=Response(
            200,
            headers={"content-type": "text/plain", "content-length": "2000"},
            text="small",
        )
    )
    reader = WebResourceReader(_settings(web_max_bytes=1000))
    ref = ResourceRef(type=ResourceType.WEB, url="https://example.com/large")

    result = await reader.read(ref, "ou_user")

    assert result.error == "网页内容过大，已按安全限制跳过读取"


@pytest.mark.asyncio
@respx.mock
async def test_read_web_resource_stops_streaming_after_size_limit() -> None:
    stream = TrackingAsyncByteStream([b"a" * 600, b"b" * 600, b"c" * 600])
    respx.get("https://example.com/chunked").mock(
        return_value=Response(
            200,
            headers={"content-type": "text/plain"},
            stream=stream,
        )
    )
    reader = WebResourceReader(_settings(web_max_bytes=1000))
    ref = ResourceRef(type=ResourceType.WEB, url="https://example.com/chunked")

    result = await reader.read(ref, "ou_user")

    assert result.error == "网页内容过大，已按安全限制跳过读取"
    assert stream.read_count == 2


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


async def _audit_details(store: SQLiteStore, event_type: str) -> list[dict[str, object]]:
    async with aiosqlite.connect(store.path) as db:
        rows = await db.execute_fetchall(
            "SELECT detail_json FROM audit_events WHERE event_type = ? ORDER BY id",
            (event_type,),
        )
    return [json.loads(str(row[0])) for row in rows]


def _text_pdf(page_texts: list[str]) -> bytes:
    font_object_id = 3 + len(page_texts) * 2
    kids = " ".join(f"{3 + index * 2} 0 R" for index in range(len(page_texts)))
    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{kids}] /Count {len(page_texts)} >>".encode(),
    ]
    for index, text in enumerate(page_texts):
        content_object_id = 4 + index * 2
        escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        stream = f"BT /F1 14 Tf 72 720 Td ({escaped}) Tj ET".encode()
        objects.extend(
            [
                (
                    "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                    f"/Resources << /Font << /F1 {font_object_id} 0 R >> >> "
                    f"/Contents {content_object_id} 0 R >>"
                ).encode(),
                f"<< /Length {len(stream)} >>\nstream\n".encode()
                + stream
                + b"\nendstream",
            ]
        )
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    result = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for object_id, body in enumerate(objects, start=1):
        offsets.append(len(result))
        result.extend(f"{object_id} 0 obj\n".encode())
        result.extend(body)
        result.extend(b"\nendobj\n")
    xref_offset = len(result)
    result.extend(f"xref\n0 {len(objects) + 1}\n".encode())
    result.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        result.extend(f"{offset:010d} 00000 n \n".encode())
    result.extend(
        (
            f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref_offset}\n%%EOF\n"
        ).encode()
    )
    return bytes(result)


def _blank_pdf() -> bytes:
    output = BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.write(output)
    return output.getvalue()
