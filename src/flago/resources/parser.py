import re
from urllib.parse import parse_qs, unquote, urlparse

from flago.models import ResourceRef, ResourceType

URL_PATTERN = re.compile(r"https?://[^\s<>()\"'，。；、！？）】》]+")
TRAILING_URL_CHARS = ".,，。)）]】>》、；;!?！？"


def extract_urls(text: str) -> list[str]:
    return [match.group(0).rstrip(TRAILING_URL_CHARS) for match in URL_PATTERN.finditer(text)]


def parse_resource_urls(text: str) -> list[ResourceRef]:
    return [parse_resource_url(url) for url in extract_urls(text)]


def parse_resource_url(url: str) -> ResourceRef:
    parsed = urlparse(url)
    host = parsed.hostname.lower() if parsed.hostname else ""
    path_parts = [unquote(part) for part in parsed.path.split("/") if part]
    query = _merged_query(parsed.query, parsed.fragment)

    if not _is_feishu_host(host):
        return ResourceRef(type=ResourceType.WEB, url=url, source_kind="web")

    if not path_parts:
        return ResourceRef(type=ResourceType.UNKNOWN, url=url)

    kind = path_parts[0].lower()
    token = path_parts[1] if len(path_parts) > 1 else None
    if kind in {"docx", "docs", "doc"} and token:
        return ResourceRef(type=ResourceType.FEISHU_DOC, url=url, source_kind=kind, token=token)
    if kind in {"sheets", "sheet"} and token:
        return ResourceRef(
            type=ResourceType.FEISHU_SHEET,
            url=url,
            source_kind=kind,
            token=token,
            sheet_id=_first(query, "sheet", "gid", "sheet_id", "sheetId"),
            range_hint=_first(query, "range"),
        )
    if kind in {"base", "bitable"} and token:
        return ResourceRef(
            type=ResourceType.FEISHU_BITABLE,
            url=url,
            source_kind=kind,
            token=token,
            table_id=_first(query, "table", "table_id", "tableId"),
            view_id=_first(query, "view", "view_id", "viewId"),
        )
    if kind == "wiki" and token:
        # Wiki links need a wiki-node lookup before doc token access; keep the type explicit.
        return ResourceRef(type=ResourceType.FEISHU_DOC, url=url, source_kind=kind, token=token)
    return ResourceRef(type=ResourceType.UNKNOWN, url=url, source_kind=kind, token=token)


def _is_feishu_host(host: str) -> bool:
    return host.endswith("feishu.cn") or host.endswith("larksuite.com")


def _merged_query(query_text: str, fragment: str) -> dict[str, list[str]]:
    query = parse_qs(query_text)
    fragment_query = fragment[1:] if fragment.startswith("?") else fragment
    if "=" in fragment_query:
        for key, values in parse_qs(fragment_query).items():
            query.setdefault(key, values)
    return query


def _first(query: dict[str, list[str]], *keys: str) -> str | None:
    for key in keys:
        values = query.get(key)
        if values:
            return values[0]
    return None
