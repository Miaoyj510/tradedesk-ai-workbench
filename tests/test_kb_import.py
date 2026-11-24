# -*- coding: utf-8 -*-
"""知识库导入（S6-5）：解析 / 校验 / 版本化 / 落库 / HTTP。"""
import json

import pytest
from fastapi.testclient import TestClient

from tradedesk.api.app import create_app
from tradedesk.api.deps import AppContext
from tradedesk.data.kb_import import import_kb, make_kb_version, parse_rows
from tradedesk.store.sqlite_store import SqliteStateStore

CSV_OK = """sku,title,text,lang,doc_type,countries,valid_until,price_low,price_high,moq
X-900,企业型号 X-900,自研手动搬运车，适合短距离搬运,zh,产品规格,CN,2027-12-31,7.10,7.80,500
X-901,企业型号 X-901,另一款产品的说明文字,zh,产品条目,CN,,,,
"""

CSV_CERT = """cert_no,title,text,doc_type,countries,valid_until
CERT-X-900,企业自检证书,自检合格,证书,CN,2028-01-31
"""

CSV_CERT_NO_DATE = """cert_no,title,text,doc_type,countries
CERT-X-901,过期证书,老证书,证书,CN
"""


@pytest.fixture
def store(tmpdir_local):
    st = SqliteStateStore(str(tmpdir_local / "kbimport.db"))
    yield st
    st.close()


@pytest.fixture
def client(tmpdir_local):
    st = SqliteStateStore(str(tmpdir_local / "kbimport_api.db"))
    c = TestClient(create_app(AppContext(store=st)))
    yield c
    st.close()


# ------------------------------------------------------------------ 解析
def test_parse_csv_returns_rows_and_columns():
    rows, cols, errors = parse_rows(CSV_OK, "csv")
    assert len(rows) == 2
    assert "sku" in cols and "price_low" in cols
    assert errors == []


def test_parse_json_accepts_array_and_wrapper():
    rows, cols, errors = parse_rows(json.dumps([{"sku": "X-1", "title": "t"}]), "json")
    assert rows[0]["sku"] == "X-1"
    rows2, _c2, _e2 = parse_rows(json.dumps({"chunks": [{"sku": "X-2", "title": "t"}]}), "json")
    assert rows2[0]["sku"] == "X-2"


def test_csv_row_with_wrong_field_count_is_rejected():
    """少一个逗号 → 整行拒收（否则列会整体左移，证书号跑进语言列）。"""
    bad = "sku,title,text,lang\nX-1,标题,正文\n"
    rows, _cols, errors = parse_rows(bad, "csv")
    assert rows == []
    assert errors and "字段数不一致" in errors[0]["message"]


def test_import_reports_row_field_count_mismatch(store):
    bad = "sku,title,text,lang\nX-1,标题,正文\n"
    rep = import_kb(store, content=bad, filename="bad.csv", dry_run=False)
    assert rep.accepted == 0 and rep.rejected == 1
    assert "字段数不一致" in rep.errors[0]["message"]
    assert rep.written == 0


# ------------------------------------------------------------------ 校验
def test_dry_run_does_not_write(store):
    rep = import_kb(store, content=CSV_OK, filename="a.csv", dry_run=True)
    assert rep.accepted == 2 and rep.rejected == 0
    assert rep.dry_run is True
    assert store.list_kb_chunks(rep.kb_version) == []


def test_import_writes_new_version_and_appears_in_version_list(store):
    rep = import_kb(store, content=CSV_OK, filename="a.csv", dry_run=False)
    assert rep.written == 2
    chunks = store.list_kb_chunks(rep.kb_version)
    assert len(chunks) == 2
    versions = {v["kb_version"]: v for v in store.list_kb_versions()}
    assert rep.kb_version in versions
    assert versions[rep.kb_version]["chunks"] == 2
    assert versions[rep.kb_version]["vectors"] == 0      # 还没建索引


def test_numeric_fields_land_in_extra_not_in_text(store):
    rep = import_kb(store, content=CSV_OK, filename="a.csv", dry_run=False)
    chunk = [c for c in store.list_kb_chunks(rep.kb_version)
             if c.identifiers.get("sku") == "X-900"][0]
    assert chunk.extra["price_low"] == 7.10
    assert chunk.extra["moq"] == 500
    # 数值不进正文（PRD 9.3：否则语义检索会返回"约等于"的数字）
    assert "7.10" not in chunk.text


def test_missing_title_and_text_is_rejected(store):
    rep = import_kb(store, content="sku,lang\nX-1,zh\n", filename="bad.csv", dry_run=False)
    assert rep.rejected == 1 and rep.accepted == 0
    assert rep.errors and rep.errors[0]["field"] == "title"


def test_certificate_without_validity_is_rejected(store):
    """合规时效：证书没有有效期 → 拒收（否则会被当作一直有效而输出过期要求）。"""
    rep = import_kb(store, content=CSV_CERT_NO_DATE, filename="cert.csv", dry_run=False)
    assert rep.accepted == 0 and rep.rejected == 1
    assert "有效期" in rep.errors[0]["message"]


def test_certificate_with_validity_is_accepted(store):
    rep = import_kb(store, content=CSV_CERT, filename="cert.csv", dry_run=False)
    assert rep.accepted == 1
    chunk = store.list_kb_chunks(rep.kb_version)[0]
    assert chunk.valid_until is not None
    assert chunk.identifiers["cert_no"] == "CERT-X-900"


def test_duplicate_chunk_id_in_same_version_is_rejected(store):
    """同一版本内块标识重复 → 报错；默认**整批不落库**（宁可改文件，也不留半新半旧的库）。"""
    dup = "sku,title,text,lang\nX-1,标题一,正文一,zh\nX-1,标题二,正文二,zh\n"
    rep = import_kb(store, content=dup, filename="dup.csv", dry_run=False)
    assert rep.accepted == 0 and rep.rejected == 2
    assert "重复" in rep.errors[0]["message"]
    assert rep.written == 0
    assert store.list_kb_chunks(rep.kb_version) == []


def test_custom_field_mapping(store):
    csv = "型号,标题,正文,语言\nX-950,企业型号 X-950,说明文字,zh\n"
    rep = import_kb(store, content=csv, filename="m.csv", fmt="csv",
                    mapping={"sku": "型号", "title": "标题", "text": "正文", "lang": "语言"},
                    dry_run=False)
    assert rep.accepted == 1
    chunk = store.list_kb_chunks(rep.kb_version)[0]
    assert chunk.identifiers["sku"] == "X-950"
    assert chunk.title == "企业型号 X-950"


def test_same_content_gives_same_version_but_new_content_differs():
    v1 = make_kb_version("aaa")
    v2 = make_kb_version("aaa")
    v3 = make_kb_version("bbb")
    assert v1 == v2 and v1 != v3
    assert v1.startswith("kb-")


def test_skip_bad_rows_keeps_good_rows(store):
    bad = "sku,title,text,lang\nX-1,,,zh\nX-2,好块,正文,zh\n"
    rep = import_kb(store, content=bad, filename="mixed.csv", dry_run=False,
                    skip_bad_rows=True)
    assert rep.accepted == 1 and rep.rejected == 1
    assert len(store.list_kb_chunks(rep.kb_version)) == 1


def test_import_leaves_other_versions_untouched(store):
    """导入是**新版本**，不覆盖旧版本（双索引能共存的前提）。"""
    first = import_kb(store, content=CSV_OK, filename="a.csv", dry_run=False)
    before = len(store.list_kb_chunks(first.kb_version))
    second = import_kb(store, content=CSV_CERT, filename="cert.csv", dry_run=False)
    assert second.kb_version != first.kb_version
    assert len(store.list_kb_chunks(first.kb_version)) == before


# ------------------------------------------------------------------ HTTP
def test_api_dry_run_is_default_and_does_not_write(client):
    r = client.post("/kb/import", json={"filename": "a.csv", "content": CSV_OK})
    assert r.status_code == 200
    body = r.json()
    assert body["dry_run"] is True
    assert body["accepted"] == 2
    assert body["written"] == 0


def test_api_real_import_and_versions_endpoint(client):
    r = client.post("/kb/import", json={"filename": "a.csv", "content": CSV_OK,
                                        "dry_run": False})
    assert r.status_code == 200
    version = r.json()["kb_version"]
    assert r.json()["written"] == 2
    v = client.get("/kb/versions")
    assert v.status_code == 200
    versions = {x["kb_version"]: x for x in v.json()["versions"]}
    assert version in versions
    assert versions[version]["chunks"] == 2


def test_api_rejects_bad_format(client):
    r = client.post("/kb/import", json={"filename": "a.txt", "content": "不是表",
                                         "format": "pdf"})
    assert r.status_code == 422


def test_api_reports_row_errors(client):
    r = client.post("/kb/import", json={"filename": "cert.csv", "content": CSV_CERT_NO_DATE})
    assert r.status_code == 200
    body = r.json()
    assert body["rejected"] == 1
    assert body["errors"]