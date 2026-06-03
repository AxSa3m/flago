import json
import logging
from typing import Any

import httpx
from bs4 import BeautifulSoup

from fcgo.config import Settings
from fcgo.feishu.openapi import FeishuOpenAPI
from fcgo.models import ResourceReadResult, ResourceRef, ResourceType

logger = logging.getLogger(__name__)


class CompositeResourceReader:
    def __init__(
        self,
        feishu_reader: "FeishuResourceReader",
        web_reader: "WebResourceReader",
    ) -> None:
        self.feishu_reader = feishu_reader
        self.web_reader = web_reader

    async def read(self, ref: ResourceRef, actor_id: str) -> ResourceReadResult:
        if ref.type == ResourceType.WEB:
            return await self.web_reader.read(ref, actor_id)
        return await self.feishu_reader.read(ref, actor_id)


class FeishuResourceReader:
    def __init__(self, settings: Settings, api: FeishuOpenAPI) -> None:
        self.settings = settings
        self.api = api

    async def read(self, ref: ResourceRef, actor_id: str) -> ResourceReadResult:
        try:
            title_hint: str | None = None
            if ref.source_kind == "wiki":
                resolved = await self._resolve_wiki_ref(ref, actor_id)
                if isinstance(resolved, ResourceReadResult):
                    return resolved
                ref, title_hint = resolved
            if ref.type == ResourceType.FEISHU_DOC:
                return await self._read_doc(ref, actor_id, title_hint=title_hint)
            if ref.type == ResourceType.FEISHU_SHEET:
                return await self._read_sheet(ref, actor_id, title_hint=title_hint)
            if ref.type == ResourceType.FEISHU_BITABLE:
                return await self._read_bitable(ref, actor_id, title_hint=title_hint)
            return ResourceReadResult(ref=ref, error="不支持的飞书资源类型")
        except Exception as exc:  # noqa: BLE001 - preserve user-facing resource errors
            logger.exception(
                "resource_read_failed type=%s token=%s source_kind=%s",
                ref.type,
                ref.token,
                ref.source_kind,
            )
            return ResourceReadResult(ref=ref, error=str(exc))

    async def _resolve_wiki_ref(
        self,
        ref: ResourceRef,
        actor_id: str,
    ) -> tuple[ResourceRef, str | None] | ResourceReadResult:
        if not ref.token:
            return ResourceReadResult(ref=ref, error="缺少飞书 Wiki token")
        node_data = await self.api.get_wiki_node(ref.token, actor_id)
        node = node_data.get("node", node_data)
        if not isinstance(node, dict):
            return ResourceReadResult(ref=ref, error="无法解析飞书 Wiki 节点")
        obj_type = _string_or_none(node.get("obj_type") or node.get("objType"))
        obj_token = _string_or_none(node.get("obj_token") or node.get("objToken"))
        title = _string_or_none(node.get("title"))
        if not obj_type:
            return ResourceReadResult(ref=ref, error="飞书 Wiki 节点缺少对象类型")
        if not obj_token:
            return ResourceReadResult(ref=ref, error="飞书 Wiki 节点缺少对象 token")

        normalized_type = obj_type.lower()
        resource_type = _wiki_obj_type_to_resource_type(normalized_type)
        if resource_type is None:
            return ResourceReadResult(
                ref=ref,
                error=f"暂不支持读取该类型的 Wiki 节点：{obj_type}",
            )
        return (
            ref.model_copy(
                update={
                    "type": resource_type,
                    "source_kind": f"wiki:{normalized_type}",
                    "token": obj_token,
                }
            ),
            title,
        )

    async def _read_doc(
        self,
        ref: ResourceRef,
        actor_id: str,
        *,
        title_hint: str | None = None,
    ) -> ResourceReadResult:
        if not ref.token:
            return ResourceReadResult(ref=ref, error="缺少飞书文档 token")
        document_token = ref.token
        data = await self.api.get_doc_raw_content(document_token, actor_id)
        content = str(data.get("content") or data.get("text") or "")
        content, truncated = _limit_text(content, self.settings.max_resource_chars)
        return ResourceReadResult(
            ref=ref,
            title=_string_or_none(data.get("title")) or title_hint,
            content=content,
            truncated=truncated,
        )

    async def _read_sheet(
        self,
        ref: ResourceRef,
        actor_id: str,
        *,
        title_hint: str | None = None,
    ) -> ResourceReadResult:
        if not ref.token:
            return ResourceReadResult(ref=ref, error="缺少飞书电子表格 token")
        sheet_id = ref.sheet_id
        sheet_title: str | None = title_hint
        if not sheet_id:
            metadata = await self.api.query_sheet_metadata(ref.token, actor_id)
            sheet = _first_visible_sheet(metadata)
            if sheet is None:
                return ResourceReadResult(ref=ref, error="飞书电子表格没有可读取的工作表")
            sheet_id = _string_or_none(sheet.get("sheet_id") or sheet.get("sheetId"))
            sheet_title = _string_or_none(sheet.get("title"))
            if not sheet_id:
                return ResourceReadResult(ref=ref, error="飞书电子表格元数据缺少 sheet_id")
        resolved_ref = ref.model_copy(update={"sheet_id": sheet_id})
        range_name = _sheet_range_name(
            resolved_ref,
            max_rows=self.settings.max_sheet_rows,
            max_columns=self.settings.max_sheet_columns,
        )
        logger.info("reading_sheet_range token=%s range=%s", ref.token, range_name)
        data = await self.api.read_sheet_range(ref.token, range_name, actor_id)
        value_range = data.get("valueRange", {})
        values = value_range.get("values", [])
        if not isinstance(values, list):
            return ResourceReadResult(ref=ref, error="飞书电子表格返回值格式异常")
        values, grid_truncated = _limit_table(
            values,
            max_rows=self.settings.max_sheet_rows,
            max_columns=self.settings.max_sheet_columns,
        )
        non_empty_cell_count = _count_non_empty_cells(values)
        append_start_row = _next_append_row(values)
        values = _trim_empty_edges(values)
        returned_range = _string_or_none(value_range.get("range")) or range_name
        content = _sheet_content(
            range_name=returned_range,
            values=values,
            non_empty_cell_count=non_empty_cell_count,
            append_start_row=append_start_row,
            grid_truncated=grid_truncated,
            max_rows=self.settings.max_sheet_rows,
            max_columns=self.settings.max_sheet_columns,
        )
        content, text_truncated = _limit_text(content, self.settings.max_resource_chars)
        return ResourceReadResult(
            ref=resolved_ref,
            title=f"{sheet_title or '电子表格'}范围 {returned_range}",
            content=content,
            truncated=grid_truncated or text_truncated,
        )

    async def _read_bitable(
        self,
        ref: ResourceRef,
        actor_id: str,
        *,
        title_hint: str | None = None,
    ) -> ResourceReadResult:
        if not ref.token:
            return ResourceReadResult(ref=ref, error="缺少多维表格 app_token")

        table_id = ref.table_id
        table_title: str | None = None
        tables: list[dict[str, Any]] = []
        if not table_id or not table_id.startswith("tbl"):
            table_data = await self.api.list_bitable_tables(ref.token, actor_id)
            tables = _dict_items(table_data)
            table = _resolve_bitable_table(tables, table_id)
            if table is None:
                return ResourceReadResult(ref=ref, error="多维表格没有可读取的数据表")
            table_id = _string_or_none(table.get("table_id") or table.get("tableId"))
            table_title = _string_or_none(
                table.get("name") or table.get("table_name") or table.get("tableName")
            )
            if not table_id:
                return ResourceReadResult(ref=ref, error="多维表格元数据缺少 table_id")

        fields: list[dict[str, Any]] = []
        views: list[dict[str, Any]] = []
        metadata_warnings: list[str] = []
        try:
            field_data = await self.api.list_bitable_fields(ref.token, table_id, actor_id)
            fields = _dict_items(field_data)
        except Exception as exc:  # noqa: BLE001 - records can still be useful without fields
            metadata_warnings.append(f"字段元数据读取失败：{exc}")
        try:
            view_data = await self.api.list_bitable_views(ref.token, table_id, actor_id)
            views = _dict_items(view_data)
        except Exception as exc:  # noqa: BLE001 - views are supplemental context
            metadata_warnings.append(f"视图元数据读取失败：{exc}")

        record_error: str | None = None
        try:
            data = await self.api.list_bitable_records(
                ref.token,
                table_id,
                actor_id,
                limit=self.settings.max_bitable_records,
                view_id=ref.view_id,
            )
        except Exception as exc:  # noqa: BLE001 - fields still allow writeback proposals
            if ref.view_id:
                try:
                    data = await self.api.list_bitable_records(
                        ref.token,
                        table_id,
                        actor_id,
                        limit=self.settings.max_bitable_records,
                        view_id=None,
                    )
                    metadata_warnings.append(f"指定视图读取失败，已回退到默认记录范围：{exc}")
                except Exception as fallback_exc:  # noqa: BLE001
                    data = {}
                    record_error = f"{exc}; 默认记录范围也读取失败：{fallback_exc}"
            else:
                data = {}
                record_error = str(exc)
        records = _bitable_records(data)
        api_truncated = bool(data.get("has_more") or data.get("hasMore"))
        if not fields:
            fields = _bitable_fields_from_record_headers(data)
        resolved_ref = ref.model_copy(update={"table_id": table_id})
        if record_error:
            metadata_warnings.append(f"记录读取失败：{record_error}")
        content = _bitable_content(
            title=title_hint,
            app_token=ref.token,
            table_id=table_id,
            table_title=table_title,
            view_id=ref.view_id,
            view_title=_resolve_bitable_view_title(views, ref.view_id),
            fields=fields,
            records=records,
            metadata_warnings=metadata_warnings,
            api_truncated=api_truncated,
            max_records=self.settings.max_bitable_records,
        )
        content, text_truncated = _limit_text(content, self.settings.max_resource_chars)
        title = title_hint or table_title
        if title and table_title and title != table_title:
            title = f"{title} - {table_title}"
        return ResourceReadResult(
            ref=resolved_ref,
            title=title or f"多维表格 {table_id}",
            content=content,
            truncated=api_truncated or text_truncated,
        )


class WebResourceReader:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def read(self, ref: ResourceRef, actor_id: str) -> ResourceReadResult:  # noqa: ARG002
        if ref.type != ResourceType.WEB:
            return ResourceReadResult(ref=ref, error="不是普通网页链接")
        try:
            async with httpx.AsyncClient(
                timeout=self.settings.web_timeout_seconds,
                follow_redirects=True,
                headers={
                    "User-Agent": "FCGO/0.1 (+https://github.com/fcgo)",
                    "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.1",
                },
            ) as client:
                response = await client.get(ref.url)
                response.raise_for_status()
            content_type = response.headers.get("content-type", "").lower()
            if content_type and not _is_readable_web_content(content_type):
                return ResourceReadResult(
                    ref=ref,
                    error=f"不支持读取该网页内容类型：{content_type.split(';', 1)[0]}",
                )
            if len(response.content) > self.settings.max_resource_chars * 4:
                return ResourceReadResult(
                    ref=ref,
                    error="网页内容过大，已按安全限制跳过读取",
                )
            soup = BeautifulSoup(response.text, "html.parser")
            for tag in soup(["script", "style", "noscript", "svg", "canvas"]):
                tag.decompose()
            title = soup.title.string.strip() if soup.title and soup.title.string else None
            text = "\n".join(
                line.strip()
                for line in soup.get_text("\n").splitlines()
                if line.strip()
            )
            text, truncated = _limit_text(text, self.settings.max_resource_chars)
            return ResourceReadResult(ref=ref, title=title, content=text, truncated=truncated)
        except Exception as exc:  # noqa: BLE001
            return ResourceReadResult(ref=ref, error=str(exc))


def _limit_text(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit] + "\n\n[内容已截断]", True


def _is_readable_web_content(content_type: str) -> bool:
    return any(
        allowed in content_type
        for allowed in (
            "text/html",
            "text/plain",
            "application/xhtml+xml",
        )
    )


def _string_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _wiki_obj_type_to_resource_type(obj_type: str) -> ResourceType | None:
    if obj_type in {"doc", "docx"}:
        return ResourceType.FEISHU_DOC
    if obj_type in {"sheet", "spreadsheet"}:
        return ResourceType.FEISHU_SHEET
    if obj_type in {"bitable", "base"}:
        return ResourceType.FEISHU_BITABLE
    return None


def _sheet_range_name(ref: ResourceRef, *, max_rows: int, max_columns: int) -> str:
    range_hint = (ref.range_hint or "").strip()
    if range_hint and "!" in range_hint:
        return range_hint
    cell_range = range_hint or f"A1:{_column_letter(max_columns)}{max_rows}"
    if ref.sheet_id:
        return f"{ref.sheet_id}!{cell_range}"
    return cell_range


def _first_visible_sheet(metadata: dict[str, Any]) -> dict[str, Any] | None:
    sheets = metadata.get("sheets", [])
    if not isinstance(sheets, list):
        return None
    candidates = [sheet for sheet in sheets if isinstance(sheet, dict)]
    for sheet in candidates:
        if sheet.get("hidden") is not True:
            return sheet
    return candidates[0] if candidates else None


def _dict_items(data: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ("items", "tables", "views", "fields", "records"):
        value = data.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def _bitable_records(data: dict[str, Any]) -> list[dict[str, Any]]:
    items = data.get("items") or data.get("records")
    if isinstance(items, list):
        return [item for item in items if isinstance(item, dict)]

    rows = data.get("data")
    headers = data.get("fields")
    record_ids = data.get("record_id_list") or data.get("recordIdList") or []
    if not isinstance(rows, list) or not isinstance(headers, list):
        return []

    field_names = [str(header) for header in headers]
    records: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        if not isinstance(row, list):
            continue
        fields = {
            field_name: value
            for field_name, value in zip(field_names, row, strict=False)
            if _has_value(value)
        }
        if not fields:
            continue
        record: dict[str, Any] = {"fields": fields}
        if index < len(record_ids):
            record["record_id"] = record_ids[index]
        records.append(record)
    return records


def _bitable_fields_from_record_headers(data: dict[str, Any]) -> list[dict[str, Any]]:
    headers = data.get("fields")
    if not isinstance(headers, list):
        return []
    field_ids = data.get("field_id_list") or data.get("fieldIdList") or []
    fields: list[dict[str, Any]] = []
    for index, header in enumerate(headers):
        if not _has_value(header):
            continue
        field: dict[str, Any] = {"field_name": str(header)}
        if isinstance(field_ids, list) and index < len(field_ids) and _has_value(field_ids[index]):
            field["field_id"] = str(field_ids[index])
        fields.append(field)
    return fields


def _resolve_bitable_table(
    tables: list[dict[str, Any]],
    table_hint: str | None,
) -> dict[str, Any] | None:
    if table_hint:
        for table in tables:
            table_id = _string_or_none(table.get("table_id") or table.get("tableId"))
            table_name = _string_or_none(
                table.get("name") or table.get("table_name") or table.get("tableName")
            )
            if table_hint in {table_id, table_name}:
                return table
    return tables[0] if tables else None


def _resolve_bitable_view_title(
    views: list[dict[str, Any]],
    view_id: str | None,
) -> str | None:
    if not view_id:
        return None
    for view in views:
        candidate_id = _string_or_none(view.get("view_id") or view.get("viewId"))
        if candidate_id == view_id:
            return _string_or_none(
                view.get("view_name") or view.get("viewName") or view.get("name")
            )
    return None


def _bitable_content(
    *,
    title: str | None,
    app_token: str,
    table_id: str,
    table_title: str | None,
    view_id: str | None,
    view_title: str | None,
    fields: list[dict[str, Any]],
    records: list[dict[str, Any]],
    metadata_warnings: list[str],
    api_truncated: bool,
    max_records: int,
) -> str:
    lines = [
        f"多维表格：{title or app_token}",
        f"数据表：{table_title or table_id}",
    ]
    if view_id:
        lines.append(f"视图：{view_title or view_id}")
    else:
        lines.append("视图：未指定，读取数据表默认记录范围")
    lines.append(f"读取记录数：{len(records)}")
    if api_truncated:
        lines.append(f"提示：记录已按安全上限截断到前 {max_records} 条。")
    if records and metadata_warnings:
        lines.append("提示：字段或视图元数据读取失败不代表记录为空；请优先根据下方记录摘录回答。")
    lines.extend(f"提示：{warning}" for warning in metadata_warnings)

    if fields:
        lines.append("")
        lines.append("字段：")
        lines.extend(_bitable_field_summaries(fields))

    lines.append("")
    if records:
        lines.append("记录索引：")
        lines.extend(_record_index_summaries(records, fields))
        lines.append("")
    lines.append("记录摘录：")
    lines.append(_records_to_markdown(records, fields) or "[没有读取到记录]")
    return "\n".join(lines)


def _bitable_field_summaries(fields: list[dict[str, Any]], *, limit: int = 50) -> list[str]:
    summaries: list[str] = []
    for field in fields[:limit]:
        name = _field_name(field)
        field_type = _string_or_none(field.get("type") or field.get("field_type"))
        summaries.append(f"- {name}" + (f"（类型：{field_type}）" if field_type else ""))
    if len(fields) > limit:
        summaries.append("- [后续字段已省略]")
    return summaries


def _records_to_markdown(
    records: list[dict[str, Any]],
    fields: list[dict[str, Any]],
    *,
    max_columns: int = 12,
) -> str:
    if not records:
        return ""
    field_names = [_field_name(field) for field in fields if _field_name(field)]
    if not field_names:
        seen: list[str] = []
        for record in records:
            record_fields = _record_fields(record)
            for key in record_fields:
                if key not in seen:
                    seen.append(key)
        field_names = seen
    field_names = field_names[:max_columns]
    if not field_names:
        return "\n".join(_cell_to_text(record) for record in records)

    rows = [
        [
            _cell_to_text(_record_fields(record).get(field_name))
            for field_name in field_names
        ]
        for record in records
    ]
    lines = [
        "| " + " | ".join(field_names) + " |",
        "| " + " | ".join(["---"] * len(field_names)) + " |",
    ]
    lines.extend("| " + " | ".join(row) + " |" for row in rows)
    return "\n".join(lines)


def _record_index_summaries(
    records: list[dict[str, Any]],
    fields: list[dict[str, Any]],
    *,
    limit: int = 50,
) -> list[str]:
    field_names = [_field_name(field) for field in fields if _field_name(field)]
    if not field_names:
        seen: list[str] = []
        for record in records:
            for key in _record_fields(record):
                if key not in seen:
                    seen.append(key)
        field_names = seen
    summaries: list[str] = []
    for index, record in enumerate(records[:limit], start=1):
        parts: list[str] = []
        record_id = _string_or_none(record.get("record_id") or record.get("recordId"))
        if record_id:
            parts.append(f"record_id={record_id}")
        record_fields = _record_fields(record)
        for field_name in field_names:
            value = record_fields.get(field_name)
            if _has_value(value):
                parts.append(f"{field_name}={_cell_to_text(value)}")
        if parts:
            summaries.append(f"- 第 {index} 条：" + "；".join(parts))
    if len(records) > limit:
        summaries.append("- [后续记录索引已省略]")
    return summaries


def _record_fields(record: dict[str, Any]) -> dict[str, Any]:
    fields = record.get("fields")
    return fields if isinstance(fields, dict) else record


def _field_name(field: dict[str, Any]) -> str:
    return (
        _string_or_none(
            field.get("field_name")
            or field.get("fieldName")
            or field.get("name")
            or field.get("field_id")
            or field.get("fieldId")
        )
        or "未命名字段"
    )


def _limit_table(
    values: list[Any],
    *,
    max_rows: int,
    max_columns: int,
) -> tuple[list[list[Any]], bool]:
    limited_rows = values[:max_rows]
    row_truncated = len(values) > max_rows
    normalized: list[list[Any]] = []
    column_truncated = False
    for row in limited_rows:
        cells = row if isinstance(row, list) else [row]
        if len(cells) > max_columns:
            column_truncated = True
        normalized.append(cells[:max_columns])
    return normalized, row_truncated or column_truncated


def _trim_empty_edges(values: list[list[Any]]) -> list[list[Any]]:
    non_empty_rows = [
        index for index, row in enumerate(values) if any(_has_value(cell) for cell in row)
    ]
    if not non_empty_rows:
        return []
    top = non_empty_rows[0]
    bottom = non_empty_rows[-1]
    row_slice = values[top : bottom + 1]

    non_empty_columns: list[int] = []
    max_cols = max((len(row) for row in row_slice), default=0)
    for column in range(max_cols):
        if any(column < len(row) and _has_value(row[column]) for row in row_slice):
            non_empty_columns.append(column)
    if not non_empty_columns:
        return []
    left = non_empty_columns[0]
    right = non_empty_columns[-1]
    return [row[left : right + 1] for row in row_slice]


def _count_non_empty_cells(values: list[list[Any]]) -> int:
    return sum(1 for row in values for cell in row if _has_value(cell))


def _next_append_row(values: list[list[Any]]) -> int:
    last_non_empty_row = 0
    for index, row in enumerate(values, start=1):
        if any(_has_value(cell) for cell in row):
            last_non_empty_row = index
    return last_non_empty_row + 1


def _has_value(value: Any) -> bool:
    return str(value).strip() != "" if value is not None else False


def _sheet_content(
    *,
    range_name: str,
    values: list[list[Any]],
    non_empty_cell_count: int,
    append_start_row: int,
    grid_truncated: bool,
    max_rows: int,
    max_columns: int,
) -> str:
    lines = [
        f"读取范围：{range_name}",
        f"非空单元格数量：{non_empty_cell_count}",
        f"建议追加起始行：{append_start_row}",
    ]
    if grid_truncated:
        lines.append(f"提示：表格已按安全上限截断到前 {max_rows} 行、{max_columns} 列。")
    if non_empty_cell_count:
        lines.append("提示：已自动裁掉外围全空行列；周边空白不代表表格为空。")
        lines.append("")
        lines.append("非空行摘录：")
        lines.extend(_non_empty_row_summaries(values))
    lines.append("")
    lines.append(_table_to_markdown(values) or "[空表格范围]")
    return "\n".join(lines)


def _non_empty_row_summaries(values: list[list[Any]], *, limit: int = 20) -> list[str]:
    summaries: list[str] = []
    for index, row in enumerate(values, start=1):
        cells = [_cell_to_text(cell) for cell in row if _has_value(cell)]
        if not cells:
            continue
        summaries.append(f"- 第 {index} 行：" + " | ".join(cells))
        if len(summaries) >= limit:
            summaries.append("- [后续非空行已省略]")
            break
    return summaries


def _table_to_markdown(values: list[list[Any]]) -> str:
    if not values:
        return ""
    rows = [[_cell_to_text(cell) for cell in row] for row in values]
    max_cols = max(len(row) for row in rows)
    padded = [row + [""] * (max_cols - len(row)) for row in rows]
    header = padded[0]
    separator = ["---"] * max_cols
    body = padded[1:]
    lines = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join(separator) + " |",
    ]
    lines.extend("| " + " | ".join(row) + " |" for row in body)
    return "\n".join(lines)


def _cell_to_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str | int | float | bool):
        text = str(value)
    else:
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return text.replace("\n", " ").replace("|", "\\|")


def _column_letter(index: int) -> str:
    index = max(1, index)
    letters = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters
