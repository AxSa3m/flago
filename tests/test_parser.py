from fcgo.models import ResourceType
from fcgo.resources.parser import extract_urls, parse_resource_url, parse_resource_urls


def test_extract_urls() -> None:
    text = "请总结 https://example.com/a 和 https://docs.feishu.cn/docx/abc123。"
    assert extract_urls(text) == ["https://example.com/a", "https://docs.feishu.cn/docx/abc123"]


def test_extract_urls_strips_common_trailing_punctuation() -> None:
    text = "看这个：https://docs.feishu.cn/docx/abc123），还有 https://example.com/a?b=1！"
    assert extract_urls(text) == ["https://docs.feishu.cn/docx/abc123", "https://example.com/a?b=1"]


def test_parse_feishu_doc_url() -> None:
    ref = parse_resource_url("https://docs.feishu.cn/docx/abc123")
    assert ref.type == ResourceType.FEISHU_DOC
    assert ref.source_kind == "docx"
    assert ref.token == "abc123"


def test_parse_feishu_wiki_url() -> None:
    ref = parse_resource_url("https://docs.feishu.cn/wiki/wikcnxxx")
    assert ref.type == ResourceType.FEISHU_DOC
    assert ref.source_kind == "wiki"
    assert ref.token == "wikcnxxx"


def test_parse_larksuite_doc_url() -> None:
    ref = parse_resource_url("https://example.larksuite.com/docs/doccnxxx")
    assert ref.type == ResourceType.FEISHU_DOC
    assert ref.source_kind == "docs"
    assert ref.token == "doccnxxx"


def test_parse_feishu_sheet_url_with_range() -> None:
    ref = parse_resource_url("https://docs.feishu.cn/sheets/shtcnxxx?sheet=abc&range=A1:B3")
    assert ref.type == ResourceType.FEISHU_SHEET
    assert ref.source_kind == "sheets"
    assert ref.token == "shtcnxxx"
    assert ref.sheet_id == "abc"
    assert ref.range_hint == "A1:B3"


def test_parse_feishu_sheet_url_with_fragment_query() -> None:
    ref = parse_resource_url("https://docs.feishu.cn/sheets/shtcnxxx#sheet=abc&range=A1%3AB3")
    assert ref.type == ResourceType.FEISHU_SHEET
    assert ref.sheet_id == "abc"
    assert ref.range_hint == "A1:B3"


def test_parse_feishu_bitable_url() -> None:
    ref = parse_resource_url("https://docs.feishu.cn/base/appabc?table=tbl123&view=vew456")
    assert ref.type == ResourceType.FEISHU_BITABLE
    assert ref.source_kind == "base"
    assert ref.token == "appabc"
    assert ref.table_id == "tbl123"
    assert ref.view_id == "vew456"


def test_parse_unknown_feishu_url() -> None:
    ref = parse_resource_url("https://docs.feishu.cn/mindnotes/abc123")
    assert ref.type == ResourceType.UNKNOWN
    assert ref.source_kind == "mindnotes"
    assert ref.token == "abc123"


def test_parse_web_url() -> None:
    ref = parse_resource_url("https://example.com/article")
    assert ref.type == ResourceType.WEB
    assert ref.source_kind == "web"


def test_parse_resource_urls() -> None:
    refs = parse_resource_urls("请看 https://docs.feishu.cn/docx/abc123 和 https://example.com/article")
    assert [ref.type for ref in refs] == [ResourceType.FEISHU_DOC, ResourceType.WEB]
