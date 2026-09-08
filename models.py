"""
模型列表管理 — 从 config.yaml + providers 获取可用模型。

职责：
  - get_available_models(): 获取当前 profile 下所有可用模型名称列表
  - get_models_by_provider(): 按 provider 分组返回模型列表
  - resolve_provider_config(): 解析指定 provider 的连接配置
  - _resolve_provider_for_model(): 根据模型 ID 反查 provider 名

（v2026.9.2 清理：validate_model_id / get_session_model 从未被调用，已移除；
后者依赖的 sessions.db 元数据结构也已随上游 SessionState 改版失效。）
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


def get_available_models() -> List[str]:
    """从 config.yaml 的 custom_providers 获取所有可用模型名称。

    返回纯模型 ID 列表（如 "deepseek/deepseek-v4-pro"），按配置顺序排列。
    """
    model_ids = []

    try:
        from hermes_cli.config import load_config
        config = load_config()

        # 1. default model
        default_model = config.get("model", {}).get("default", "")
        if default_model:
            model_ids.append(default_model)

        # 2. custom_providers 的 models 白名单
        custom_providers = config.get("custom_providers", [])
        for cp in custom_providers:
            models_map = cp.get("models", {})
            if isinstance(models_map, dict):
                model_ids.extend(models_map.keys())
            elif isinstance(models_map, list):
                for m in models_map:
                    if isinstance(m, dict):
                        model_ids.append(m.get("name", m.get("id", "")))
                    elif isinstance(m, str):
                        model_ids.append(m)
            elif cp.get("model"):
                model_ids.append(cp["model"])

        # 去重 + 保持插入顺序
        seen = set()
        unique = []
        for m in model_ids:
            if m and m not in seen:
                seen.add(m)
                unique.append(m)
        return unique

    except Exception as e:
        logger.error("Failed to load models from config: %s", e)
        return []


def get_models_by_provider() -> List[Tuple[str, str, List[str]]]:
    """按 provider 分组返回模型列表。

    返回: [(provider_name, display_label, [model_id, ...]), ...]
    例: [("zenmux", "🔵 ZenMux", ["deepseek/deepseek-v4-pro", ...])]

    同一 provider 内模型按配置顺序排列（即按价格排序）。
    """
    groups: Dict[str, List[str]] = {}
    provider_labels: Dict[str, str] = {}

    try:
        from hermes_cli.config import load_config
        config = load_config()

        default_model = config.get("model", {}).get("default", "")
        default_provider = config.get("model", {}).get("provider", "")

        # custom_providers
        for cp in config.get("custom_providers", []):
            name = cp.get("name", "") or cp.get("slug", "")
            display = cp.get("display_name", "") or name
            is_default = (default_provider in (name, f"custom:{name}"))
            marker = " ⚙️" if is_default else ""
            label = f"🔵 {display}{marker}" if display else f"🔵 {name}{marker}"

            models_map = cp.get("models", {})
            model_list = []
            if isinstance(models_map, dict):
                model_list = list(models_map.keys())
            elif isinstance(models_map, list):
                for m in models_map:
                    if isinstance(m, dict):
                        model_list.append(m.get("name", m.get("id", "")))
                    elif isinstance(m, str):
                        model_list.append(m)

            # 如果 default_model 不在此 provider 的 models 中但 provider 是 default，追加到头部
            if is_default and default_model and default_model not in model_list:
                model_list.insert(0, default_model)

            if model_list:
                seen = set()
                unique = []
                for m in model_list:
                    if m and m not in seen:
                        seen.add(m)
                        unique.append(m)
                groups[name] = unique
                provider_labels[name] = label

        # 如果 default provider 不在 custom_providers 中，单独建组
        # 注意：custom:xxx 格式会被 Hermes 内部使用，需要跳过已匹配的
        if default_model and default_provider not in groups:
            # 检查 custom:xxx 格式是否已匹配
            _matched = False
            if default_provider.startswith("custom:"):
                _bare = default_provider[7:]
                if _bare in groups:
                    _matched = True
            if not _matched:
                key = default_provider or "default"
                groups[key] = [default_model]
                provider_labels[key] = f"⚙️ {key.title()} (default)"

    except Exception as e:
        logger.error("Failed to load models by provider: %s", e)

    return [(k, provider_labels.get(k, k), v) for k, v in groups.items()]


def resolve_provider_config(provider_name: str) -> Optional[Dict[str, str]]:
    """从 custom_providers 中解析指定 provider 的连接配置。

    返回: {"base_url": ..., "api_key": ..., "api_mode": ..., "provider": ...}
    如果 provider_name 是 default model 的 provider，从 model config 中读取。
    """
    try:
        from hermes_cli.config import load_config
        config = load_config()

        # 优先查 custom_providers
        for cp in config.get("custom_providers", []):
            name = cp.get("name", "") or cp.get("slug", "")
            if name == provider_name:
                api_key = cp.get("api_key", "")
                # api_key 可能是 ${ENV_VAR} 格式
                if api_key and api_key.startswith("${") and api_key.endswith("}"):
                    import os
                    api_key = os.environ.get(api_key[2:-1], "")
                return {
                    "base_url": cp.get("base_url", ""),
                    "api_key": api_key,
                    "api_mode": cp.get("api_mode", "") or "chat_completions",
                    "provider": f"custom:{name}",
                }

        # 回退到 model config (default provider)
        model_cfg = config.get("model", {})
        if model_cfg.get("provider") == provider_name or model_cfg.get("provider") == f"custom:{provider_name}":
            return {
                "base_url": model_cfg.get("base_url", ""),
                "api_key": "",  # default provider 的 key 由 runtime 解析
                "api_mode": "chat_completions",
                "provider": model_cfg.get("provider", provider_name),
            }

    except Exception as e:
        logger.error("Failed to resolve provider config for %s: %s", provider_name, e)

    return None


def _resolve_provider_for_model(model_id: str) -> str:
    """根据 model_id 解析 provider name。

    策略：
      1. 遍历 custom_providers，查找 models 字段中包含该 model_id 的 provider
      2. 匹配 name 字段（非 slug）
      3. 回退到 model_id 前缀
    """
    try:
        from hermes_cli.config import load_config
        config = load_config()

        custom_providers = config.get("custom_providers", [])
        for cp in custom_providers:
            name = cp.get("name", "") or cp.get("slug", "")
            models_map = cp.get("models", {})
            if isinstance(models_map, dict) and model_id in models_map:
                return name
            if cp.get("model") == model_id:
                return name

        # model_id 含 "/" 时，前缀可能是 provider 名
        if "/" in model_id:
            prefix = model_id.split("/")[0]
            for cp in custom_providers:
                name = cp.get("name", "") or cp.get("slug", "")
                if name == prefix:
                    return name

    except Exception as e:
        logger.debug("Failed to resolve provider for model %s: %s", model_id, e)

    return ""
