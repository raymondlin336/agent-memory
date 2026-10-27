# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""配置层加载：读一层 YAML/JSON 文件并展开 ``${VAR}`` / ``${VAR:-默认值}``。

由 HTTP / MCP 两个 surface 共享——连接串与密钥经环境变量注入，配置文件本身不落密。
（CLI 直接传 JSON 层，不经此模块。）
"""

from __future__ import annotations

import json
import os
import re

# ${VAR} 或 ${VAR:-默认值}
_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def _sub_env(m: re.Match[str]) -> str:
    name, default = m.group(1), m.group(2)
    return os.environ.get(name, default if default is not None else "")


def expand_env(obj):
    """递归把字符串叶子里的 ``${VAR}`` / ``${VAR:-默认}`` 用环境变量展开。"""
    if isinstance(obj, str):
        return _ENV_RE.sub(_sub_env, obj)
    if isinstance(obj, dict):
        return {k: expand_env(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [expand_env(v) for v in obj]
    return obj


def load_layer(path: str) -> dict:
    """读一层配置文件：``.yml/.yaml`` 走 YAML，其余按 JSON；读后做环境变量展开。

    可选继承：顶层 ``extends`` 指向基础配置（相对本文件），``drop_memory_api`` 列出要从基础
    ``memory_api`` 删掉的命名空间（回落内核默认，如内存存储），其余键递归覆盖到基础之上。
    """
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    if path.endswith((".yml", ".yaml")):
        import yaml  # 部署镜像已装 PyYAML（见 pyproject 的 deploy extra）

        data = yaml.safe_load(text) or {}
    else:
        data = json.loads(text) if text.strip() else {}
    base_ref = data.pop("extends", None)
    drop = data.pop("drop_memory_api", None) or []
    if base_ref:
        base = load_layer(os.path.join(os.path.dirname(os.path.abspath(path)), base_ref))
        for namespace in drop:
            base.get("memory_api", {}).pop(namespace, None)
        return _merge(base, expand_env(data))
    return expand_env(data)


def _merge(base: dict, override: dict) -> dict:
    """递归合并：映射逐键合并，其余值由 ``override`` 整体替换。"""
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge(merged[key], value)
        else:
            merged[key] = value
    return merged
