# -*- coding: utf-8 -*-
"""测试公共配置：路径注入 + 自建临时目录 fixture。"""
import os
import shutil
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

TMP_ROOT = ROOT / "data" / "tmp"


@pytest.fixture
def tmpdir_local():
    """项目内的独立临时目录。

    不用 pytest 的 tmp_path：它的 basetemp 在启动时会被 rmtree，
    受限 ACL 下会 PermissionError；这里自建自销，失败也不影响结果。
    """
    d = TMP_ROOT / f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
    d.mkdir(parents=True, exist_ok=True)
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)