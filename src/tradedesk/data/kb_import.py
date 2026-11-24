# -*- coding: utf-8 -*-
"""S6-5 知识库导入：CSV / JSON → 校验 → 新的库版本。

为什么要有这个
--------------
Demo 阶段知识库是**代码里的种子**（`data/kb_seed.py`）。真实场景里，
企业的产品资料、报价单、证书、法规是**他们自己的文件**，必须能进得来 ——
否则"多语种知识库"永远只是演示。

它为什么不是"上传完就能用"
------------------------
导入只是第一步，真正要守住的是三条：

1. **可回指**：每块要有标识（证书号 / 条款号 / SKU），否则回答没法引用到具体条目；
2. **合规时效**：证书与法规**必须有有效期** —— 检索要按「买家所在国 + 当前日期」
   过滤，缺有效期就等于默许输出过期要求；
3. **结构化分流**：带数值的行 → 结构化规格块（走属性过滤）；
   纯文本行 → 语义召回块（证书 / 话术 / 说明）。混在一起结构化通道就没有意义。

导入产出的是**一个新的 kb_version**，不覆盖旧版本 —— 所以后面可以直接走
发布流程灰度出去，出问题按 knowledge 类别回滚（双索引 + 别名切换，不重建）。
"""
from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from ..domain.models import KBChunk

# 目标字段 → 源列名（默认同名）。mapping 里可以改成企业自己的列名。
DEFAULT_MAPPING: Dict[str, str] = {
    "title": "title",
    "text": "text",
    "doc_id": "doc_id",
    "doc_type": "doc_type",
    "lang": "lang",
    "chunk_id": "chunk_id",
    "sku": "sku",
    "hs_code": "hs_code",
    "cert_no": "cert_no",
    "clause_no": "clause_no",
    "countries": "countries",
    "tags": "tags",
    "effective_from": "effective_from",
    "effective_to": "effective_to",
    "valid_until": "valid_until",
    "extra": "extra",
}

# 这些列会被收进 extra —— 数值类字段走**属性过滤**，不写进正文文本
# （PRD 9.3：数值不进文本块，否则语义检索会返回"约等于"的数字）。
NUMERIC_FIELDS = ("price_low", "price_high", "moq", "lead_time_days", "capacity_kg")
TEXT_FIELDS = ("terms", "port", "material", "capacity", "notes")

# 必须带有效期的块类型（合规时效：检索要按"买家所在国 + 当前日期"过滤）
TIMED_DOC_TYPES = ("证书", "法规条款", "名单条目")

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@dataclass
class ImportReport:
    """导入结果（干跑与真导入返回同一个形状，便于前端对比）。"""

    kb_version: str = ""
    filename: str = ""
    format: str = ""
    total_rows: int = 0
    accepted: int = 0
    rejected: int = 0
    dry_run: bool = True
    errors: List[Dict[str, Any]] = field(default_factory=list)
    warnings: List[Dict[str, Any]] = field(default_factory=list)
    chunks: List[KBChunk] = field(default_factory=list)
    written: int = 0

    def as_dict(self, include_chunks: bool = False) -> Dict[str, Any]:
        out = {
            "kb_version": self.kb_version,
            "filename": self.filename,
            "format": self.format,
            "total_rows": self.total_rows,
            "accepted": self.accepted,
            "rejected": self.rejected,
            "dry_run": self.dry_run,
            "written": self.written,
            "errors": self.errors,
            "warnings": self.warnings,
        }
        if include_chunks:
            out["chunks"] = [c.model_dump(mode="json") for c in self.chunks]
        return out


def _err(row_no: int, field_name: str, message: str) -> Dict[str, Any]:
    return {"row": row_no, "field": field_name, "message": message}


def _warn(row_no: int, field_name: str, message: str) -> Dict[str, Any]:
    return {"row": row_no, "field": field_name, "message": message}


def guess_format(filename: str, content: str) -> str:
    """猜文件格式：扩展名优先，其次看内容开头。"""
    low = (filename or "").lower()
    if low.endswith(".csv"):
        return "csv"
    if low.endswith(".json"):
        return "json"
    head = (content or "").lstrip()[:1]
    if head in ("[", "{"):
        return "json"
    return "csv"


def parse_rows(content: str, fmt: str) -> Tuple[List[Dict[str, Any]], List[str], List[Dict[str, Any]]]:
    """把内容解析成行列表 + 列名列表 + 行级错误。

    **字段数与表头不一致必须报错**：CSV 少一个逗号，DictReader 会把后面的值
    整体左移（证书号跑到语言列里），表面"导入成功"、实际数据全错位 ——
    企业自己维护的 CSV 少个逗号是常事，所以这里宁可拒收。
    """
    if fmt == "csv":
        text = (content or "").lstrip("\ufeff")
        reader = csv.reader(io.StringIO(text))
        header = next(reader, [])
        rows: List[Dict[str, Any]] = []
        errors: List[Dict[str, Any]] = []
        row_no = 0
        for raw in reader:
            if not any((c or "").strip() for c in raw):
                continue                      # 空行跳过（不占行号）
            row_no += 1
            if len(raw) != len(header):
                errors.append(_err(row_no, "csv",
                                   "该行有 %d 个字段、表头有 %d 个 —— 字段数不一致会造成列错位"
                                   "（企业自己维护的 CSV 少个逗号是常事，请补齐）"
                                   % (len(raw), len(header))))
                continue
            rows.append({k: ("" if v is None else v) for k, v in zip(header, raw)})
        return rows, list(header), errors
    # json
    data = json.loads(content)
    if isinstance(data, dict):
        for key in ("chunks", "rows", "items", "data"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
        else:
            raise ValueError("JSON 顶层必须是数组，或含 chunks / rows / items / data 数组")
    if not isinstance(data, list):
        raise ValueError("JSON 顶层不是数组")
    rows = []
    for item in data:
        if not isinstance(item, dict):
            raise ValueError("JSON 数组的元素必须是对象")
        rows.append({k: ("" if v is None else v) for k, v in item.items()})
    cols: List[str] = []
    for r in rows:
        for k in r:
            if k not in cols:
                cols.append(k)
    return rows, cols, []


def _pick(row: Dict[str, Any], mapping: Dict[str, str], key: str) -> Any:
    src = mapping.get(key, DEFAULT_MAPPING.get(key, key))
    v = row.get(src)
    if v is None:
        return ""
    if isinstance(v, str):
        return v.strip()
    return v


def _split_list(v: Any) -> List[str]:
    if v is None:
        return []
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    s = str(v).strip()
    if not s:
        return []
    if s.startswith("["):
        try:
            arr = json.loads(s)
            return [str(x).strip() for x in arr if str(x).strip()]
        except Exception:
            pass
    return [x.strip() for x in re.split(r"[,;|]", s) if x.strip()]


def _parse_date(v: Any, row_no: int, name: str, errors: List[Dict[str, Any]]) -> Optional[datetime]:
    if v in (None, ""):
        return None
    s = str(v).strip()
    if not _DATE_RE.match(s):
        errors.append(_err(row_no, name, "日期格式必须是 YYYY-MM-DD，收到 %r" % s))
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        errors.append(_err(row_no, name, "日期无效：%r" % s))
        return None


def _build_extra(row: Dict[str, Any], mapping: Dict[str, str], row_no: int,
                 errors: List[Dict[str, Any]], warnings: List[Dict[str, Any]]) -> Dict[str, Any]:
    extra: Dict[str, Any] = {}
    raw_extra = _pick(row, mapping, "extra")
    if raw_extra:
        try:
            parsed = json.loads(raw_extra) if isinstance(raw_extra, str) else raw_extra
            if isinstance(parsed, dict):
                extra.update(parsed)
            else:
                warnings.append(_warn(row_no, "extra", "extra 不是 JSON 对象，已忽略"))
        except Exception:
            errors.append(_err(row_no, "extra", "extra 不是合法 JSON 对象"))
    for key in NUMERIC_FIELDS + TEXT_FIELDS:
        v = _pick(row, mapping, key)
        if v in (None, ""):
            continue
        if key in NUMERIC_FIELDS:
            try:
                extra[key] = float(v) if "." in str(v) else int(str(v))
            except ValueError:
                errors.append(_err(row_no, key, "数值字段解析失败：%r" % v))
        else:
            extra[key] = str(v)
    return extra


def row_to_chunk(row: Dict[str, Any], mapping: Dict[str, str], kb_version: str,
                 row_no: int) -> Tuple[Optional[KBChunk], List[Dict[str, Any]], List[Dict[str, Any]]]:
    """一行 → 一个知识块（带校验）。返回 (块, 错误, 警告)。"""
    errors: List[Dict[str, Any]] = []
    warnings: List[Dict[str, Any]] = []

    title = str(_pick(row, mapping, "title") or "")
    text = str(_pick(row, mapping, "text") or "")
    if not title and not text:
        errors.append(_err(row_no, "title", "标题与正文至少要有一个"))
        return None, errors, warnings

    identifiers: Dict[str, str] = {}
    sku = str(_pick(row, mapping, "sku") or "")
    hs_code = str(_pick(row, mapping, "hs_code") or "")
    cert_no = str(_pick(row, mapping, "cert_no") or "")
    clause_no = str(_pick(row, mapping, "clause_no") or "")
    if sku:
        identifiers["sku"] = sku
    if hs_code:
        identifiers["hs_code"] = hs_code
    if cert_no:
        identifiers["cert_no"] = cert_no
    if clause_no:
        identifiers["clause_no"] = clause_no

    extra = _build_extra(row, mapping, row_no, errors, warnings)

    # 块类型：显式指定 > 按标识推断 > 按内容推断
    doc_type = str(_pick(row, mapping, "doc_type") or "")
    if not doc_type:
        if cert_no:
            doc_type = "证书"
        elif clause_no:
            doc_type = "法规条款"
        elif any(k in extra for k in NUMERIC_FIELDS):
            doc_type = "产品规格"
        else:
            doc_type = "产品条目"

    # 时效：证书 / 法规 / 名单条目必须有有效期，否则检索无法按当前日期过滤
    effective_from = _parse_date(_pick(row, mapping, "effective_from"), row_no, "effective_from", errors)
    effective_to = _parse_date(_pick(row, mapping, "effective_to"), row_no, "effective_to", errors)
    valid_until = _parse_date(_pick(row, mapping, "valid_until"), row_no, "valid_until", errors)
    if doc_type in TIMED_DOC_TYPES and not (valid_until or effective_to):
        errors.append(_err(row_no, "valid_until",
                           "%s 必须带有效期（valid_until 或 effective_to）："
                           "检索要按「买家所在国 + 当前日期」过滤，缺有效期等于允许输出过期要求"
                           % doc_type))
        return None, errors, warnings
    if valid_until and effective_to and valid_until < effective_to:
        warnings.append(_warn(row_no, "valid_until", "有效期早于废止日期，按有效期为准"))

    # 可回指标识：没有标识就引用不到具体条目
    doc_id = str(_pick(row, mapping, "doc_id") or "")
    chunk_id = str(_pick(row, mapping, "chunk_id") or "")
    if not chunk_id:
        chunk_id = cert_no or clause_no or sku or doc_id
    if not chunk_id:
        errors.append(_err(row_no, "chunk_id",
                           "缺少可回指标识（chunk_id / 证书号 / 条款号 / SKU / doc_id 至少给一个）"))
        return None, errors, warnings
    if not doc_id:
        doc_id = chunk_id

    lang = str(_pick(row, mapping, "lang") or "").lower()
    if not lang:
        lang = "zh"
        warnings.append(_warn(row_no, "lang", "未标语言，默认 zh（多语种检索按块语言过滤）"))
    if lang not in ("zh", "en", "es", "ar"):
        warnings.append(_warn(row_no, "lang", "语种 %r 不在支持范围（zh/en/es/ar）" % lang))

    identifiers.setdefault("doc_ref", doc_id)

    chunk = KBChunk(
        chunk_id=chunk_id,
        doc_id=doc_id,
        doc_type=doc_type,
        title=title or doc_id,
        text=text,
        lang=lang,
        identifiers=identifiers,
        countries=_split_list(_pick(row, mapping, "countries")),
        hs_codes=[hs_code] if hs_code else [],
        tags=_split_list(_pick(row, mapping, "tags")),
        effective_from=effective_from,
        effective_to=effective_to,
        valid_until=valid_until,
        kb_version=kb_version,
        extra=extra,
    )
    return chunk, errors, warnings


def make_kb_version(content: str, explicit: str = "") -> str:
    """版本号：同内容同版本（幂等重导不会到处生版本），不同内容不同版本。"""
    if explicit:
        return explicit.strip()
    digest = hashlib.sha256((content or "").encode("utf-8")).hexdigest()[:6]
    return "kb-%s-%s" % (datetime.now(timezone.utc).strftime("%Y.%m"), digest)


def import_kb(store, *, content: str, filename: str = "", fmt: str = "auto",
              mapping: Optional[Dict[str, str]] = None,
              kb_version: str = "", actor: str = "local",
              dry_run: bool = True, skip_bad_rows: bool = False) -> ImportReport:
    """导入企业知识库 → 一个新的 kb_version。

    `dry_run=True`（默认）只做解析与校验、不落库 —— 导入是破坏性动作，
    必须先让人看一眼校验报告再确认。
    """
    content = content or ""
    if not content.strip():
        raise ValueError("导入内容为空")
    fmt = (fmt or "auto").lower()
    if fmt == "auto":
        fmt = guess_format(filename, content)
    if fmt not in ("csv", "json"):
        raise ValueError("只支持 csv / json 两种格式（收到 %r）" % fmt)

    mp = dict(DEFAULT_MAPPING)
    mp.update(mapping or {})

    rows, cols, row_errors = parse_rows(content, fmt)
    version = make_kb_version(content, kb_version)

    # 总行数要**含解析阶段就被拒的行**（比如 CSV 字段数不符）：
    # 否则报告会写着"拒绝 0 行"却带着错误，自相矛盾。
    report = ImportReport(kb_version=version, filename=filename, format=fmt,
                          total_rows=len(rows) + len(row_errors), dry_run=dry_run)
    report.errors.extend(row_errors)
    report.rejected += len(row_errors)
    seen: Dict[str, int] = {}
    for idx, row in enumerate(rows, start=1):
        chunk, errors, warnings = row_to_chunk(row, mp, version, idx)
        report.errors.extend(errors)
        report.warnings.extend(warnings)
        if chunk is None:
            report.rejected += 1
            continue
        if chunk.chunk_id in seen:
            report.rejected += 1
            report.errors.append(_err(idx, "chunk_id",
                                      "块标识 %r 与第 %d 行重复（同一版本内标识必须唯一）"
                                      % (chunk.chunk_id, seen[chunk.chunk_id])))
            continue
        seen[chunk.chunk_id] = idx
        report.chunks.append(chunk)
        report.accepted += 1

    if report.errors and not skip_bad_rows:
        # 有错就不落库：宁可让用户改文件，也不要落一半
        report.chunks = []
        report.accepted = 0
        report.rejected = report.total_rows
        return report

    if dry_run:
        return report

    for c in report.chunks:
        store.upsert_kb_chunk(c)
    report.written = len(report.chunks)
    try:
        store.append_audit(actor, "kb.import", version, {
            "filename": filename,
            "format": fmt,
            "total_rows": report.total_rows,
            "accepted": report.accepted,
            "rejected": report.rejected,
            "chunk_ids": [c.chunk_id for c in report.chunks][:200],
            "mapping": {k: v for k, v in mp.items() if DEFAULT_MAPPING.get(k) != v},
            "skipped_bad_rows": bool(skip_bad_rows),
        })
    except Exception:
        pass
    return report


def decode_content(content: str = "", content_b64: str = "") -> str:
    """内容既可以传明文，也可以传 base64（二进制文件）。"""
    if content_b64:
        return base64.b64decode(content_b64).decode("utf-8", "replace")
    return content or ""