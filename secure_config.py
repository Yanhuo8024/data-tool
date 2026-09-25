#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
secure_config.py - 加密API密钥管理 (Fernet/AES)
"""
import os, json, hashlib, base64, logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger('data.tool')

SECURE_DIR = Path(__file__).parent / ".secure"
SECURE_FILE = SECURE_DIR / "credentials.enc"

# 与 app.py 共用的持久化随机密钥文件
SECRET_KEY_FILE = Path(__file__).parent / "data" / "secret.key"


def _load_or_create_persisted_secret() -> str:
    """读取或生成持久化随机密钥（data/secret.key），保证重启后加密数据可解密"""
    import secrets as _secrets
    try:
        SECRET_KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
        if SECRET_KEY_FILE.exists():
            key = SECRET_KEY_FILE.read_text(encoding="utf-8").strip()
            if key:
                return key
        key = _secrets.token_hex(32)
        SECRET_KEY_FILE.write_text(key, encoding="utf-8")
        return key
    except Exception:
        # 无法读写密钥文件时退化为随机密钥（进程级有效）
        return _secrets.token_hex(32)

try:
    from cryptography.fernet import Fernet
    _HAS_FERNET = True
except ImportError:
    _HAS_FERNET = False


class SecureConfig:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        # 密钥来源优先级：环境变量 > 持久化密钥文件 > 随机生成
        # 避免硬编码弱默认值导致加密形同虚设
        secret = (os.environ.get("APP_SECRET")
                  or os.environ.get("FLASK_SECRET_KEY")
                  or os.environ.get("SECRET_KEY")
                  or _load_or_create_persisted_secret())
        key = hashlib.sha256(secret.encode()).digest()
        if _HAS_FERNET:
            self._cipher = Fernet(base64.urlsafe_b64encode(key[:32]))
        else:
            # 缺 cryptography 时不可静默降级：早期实现让 _load() 抛异常被吞成 {}，
            # 表现为「Key 明明存过却读不到」，调用方只看到「未配置」。
            self._cipher = None
            logger.warning(
                "cryptography 未安装：加密凭据库 %s 不可用，已保存的 API Key 将无法读取。"
                " 修复：python -m pip install cryptography",
                SECURE_FILE.name,
            )
        SECURE_DIR.mkdir(mode=0o700, exist_ok=True)
        self._data = {}
        self._decrypt_error = None
        self._load()
        self._initialized = True

    def _load(self):
        if not SECURE_FILE.exists():
            return
        if self._cipher is None:
            logger.warning("凭据文件 %s 存在，但加密后端不可用，已跳过加载", SECURE_FILE.name)
            return
        try:
            self._data = json.loads(self._cipher.decrypt(SECURE_FILE.read_bytes()).decode())
        except Exception as e:
            # 解密失败通常意味着 data/secret.key 被更换 —— 此时整个凭据库失效。
            # 静默置空会把「Key 已保存」变成假象，必须留下显式告警。
            self._data = {}
            self._decrypt_error = f"{type(e).__name__}: {e}"
            logger.warning(
                "凭据文件 %s 解密失败（%s），已保存的凭据均不可用；"
                "常见原因：data/secret.key 被更换或不同副本混用",
                SECURE_FILE.name, e,
            )

    def _save(self):
        if self._cipher is None:
            raise RuntimeError(
                "加密凭据库不可用（cryptography 未安装），拒绝静默丢弃 API Key。"
                " 请执行: python -m pip install cryptography"
            )
        SECURE_FILE.write_bytes(self._cipher.encrypt(json.dumps(self._data).encode()))
        SECURE_FILE.chmod(0o600)

    def set(self, section, key, value):
        self._data.setdefault(section, {})[key] = value
        self._save()

    def get(self, section, key, default=None):
        return self._data.get(section, {}).get(key, default)

    def set_token(self, name, token):
        self.set("tokens", name, token)

    def get_token(self, name):
        return self.get("tokens", name)


secure_config = SecureConfig()


def get_tushare_token() -> Optional[str]:
    return secure_config.get_token("tushare") or os.environ.get("TUSHARE_TOKEN")


def get_llm_api_key(provider: str) -> Optional[str]:
    """从加密凭据库读取 LLM API Key（.secure/credentials.enc，键 tokens/<PROVIDER>_API_KEY）。

    优先级由 llm_client._resolve_api_key 决定：环境变量 > 本加密存储 > 前端传入。
    """
    try:
        return secure_config.get_token(f"{provider.upper()}_API_KEY")
    except Exception:
        return None


def set_llm_api_key(provider: str, key: str) -> None:
    """将 LLM API Key 写入加密凭据库（Fernet/AES，密钥源自 data/secret.key）

    加密后端不可用时**抛错而非静默丢弃** —— 早期实现会让调用方以为保存成功，
    实际 key 从未落盘（表现为反复 401）。
    """
    if not key:
        raise ValueError("API Key 为空，拒绝写入")
    secure_config.set_token(f"{provider.upper()}_API_KEY", key)
    # 读回校验：确保密文确实可解密（secret.key 与凭据文件不匹配时能当场暴露）
    if secure_config.get_token(f"{provider.upper()}_API_KEY") != key:
        raise RuntimeError("API Key 写入后读回校验失败，凭据库可能损坏")


def credentials_backend_ok() -> bool:
    """加密后端是否可用（诊断用）。False 表示 .secure 凭据库读写均不可靠。"""
    return getattr(secure_config, "_cipher", None) is not None


def credentials_diagnostic() -> dict:
    """凭据库自检信息：便于 /健康检查 类端点与排查脚本直接复用。"""
    err = getattr(secure_config, "_decrypt_error", None)
    return {
        "backend_ok": credentials_backend_ok(),
        "file_exists": SECURE_FILE.exists(),
        "decrypt_error": err,
        "providers": sorted(
            k[:-len("_API_KEY")].lower()
            for k in (secure_config._data.get("tokens") or {})
            if k.endswith("_API_KEY")
        ),
        "secure_file": str(SECURE_FILE),
        "hint": (
            "cryptography 未安装，先执行 python -m pip install cryptography"
            if not credentials_backend_ok()
            else ("secret.key 与凭据文件不匹配，需重新写入 Key" if err else "正常")
        ),
    }
