# -*- coding: utf-8 -*-
"""
prepare_github_upload.py
========================
生成可直接拖进 GitHub 网页「Upload files」的干净目录：

    github_upload/

原则：
- 保留本地正确的多层结构（routes/ templates/ static/ ...），不拍平
- 不包含 python-portable、output、密钥、日志、__pycache__
- 用法：双击 upload_to_github.bat，或：
      python prepare_github_upload.py
  然后打开 GitHub 仓库 → Add file → Upload files → 把 github_upload 里内容拖进去
"""
from __future__ import annotations

import shutil
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "github_upload"

# 必须打包的文件（根目录）
ROOT_FILES = [
    "README.md",
    "requirements.txt",
    "requirements.lock.txt",
    "install.bat",
    "install.ps1",
    "start.bat",
    "stop.bat",
    "一键启动.bat",
    "pytest.ini",
    ".gitignore",
    ".env.example",
    "app.py",
    "core_config.py",
    "run_server.py",
    "run_worker.py",
    "launcher.py",
]

# 根目录业务模块：*.py 全收（排除 _ 探针）
# 工具/配置类非 py 文件按名单
ROOT_EXTS_INCLUDE = {".py", ".md", ".bat", ".ini", ".txt"}

# 目录：整树复制（内部再按排除规则过滤）
INCLUDE_DIRS = [
    "routes",
    "templates",
    "static",
    "tests",
    "tools",
    "skills",
    "knowledge_scenarios",
    "design",
    "scripts",
    "plugins",
    "rules",
]

# 目录内排除片段
EXCLUDE_PARTS = (
    "__pycache__",
    ".git",
    ".pytest_cache",
    "node_modules",
    ".workbuddy",
    "python-portable",
)

EXCLUDE_SUFFIX = (".pyc", ".pyo", ".log", ".pkl", ".model", ".whl", ".zip")
EXCLUDE_NAMES = {
    ".env", "secret.key", "credentials.enc", "db_secret.key",
    "Thumbs.db", "desktop.ini", ".DS_Store",
}


def _ok_file(p: Path) -> bool:
    name = p.name
    if name in EXCLUDE_NAMES:
        return False
    if p.suffix.lower() in EXCLUDE_SUFFIX:
        return False
    parts = set(p.parts)
    if parts & set(EXCLUDE_PARTS):
        return False
    return True


def _copy_file(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)


def _copy_tree_filtered(src_dir: Path, dest_dir: Path) -> int:
    n = 0
    for p in src_dir.rglob("*"):
        if not p.is_file():
            continue
        if not _ok_file(p):
            continue
        rel = p.relative_to(src_dir)
        _copy_file(p, dest_dir / rel)
        n += 1
    return n


def build() -> None:
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)

    copied = 0
    # 1) 指定根文件
    for name in ROOT_FILES:
        src = ROOT / name
        if src.is_file():
            _copy_file(src, OUT / name)
            copied += 1
        else:
            print("  [skip] 缺少", name)

    # 2) 根目录其余 .py（完整后端业务模块）
    for p in sorted(ROOT.glob("*.py")):
        if p.name.startswith("_"):
            continue
        if p.name == "prepare_github_upload.py":
            # 自己也带上，方便他人重打包
            _copy_file(p, OUT / p.name)
            copied += 1
            continue
        _copy_file(p, OUT / p.name)
        copied += 1

    # 3) 其它根文档/脚本
    for p in sorted(ROOT.iterdir()):
        if p.is_dir():
            continue
        if p.name in ROOT_FILES:
            continue
        if p.suffix.lower() in {".md", ".bat", ".ps1"} and _ok_file(p):
            _copy_file(p, OUT / p.name)
            copied += 1

    # 4) 目录树
    for dname in INCLUDE_DIRS:
        src = ROOT / dname
        if not src.is_dir():
            print("  [skip] 无目录", dname)
            continue
        n = _copy_tree_filtered(src, OUT / dname)
        print(f"  [dir] {dname}/ +{n}")
        copied += n

    # 5) 顶层说明
    (OUT / "UPLOAD.md").write_text(
        """# 如何上传到 GitHub

1. 打开你的仓库页面（例如 https://github.com/Yanhuo8024/data-tool ）
2. 点击 **Add file → Upload files**
3. 把本文件夹（`github_upload`）**里面的所有内容**拖进上传区  
   （保持 `routes/`、`templates/`、`static/` 等文件夹结构，不要拍平）
4. 填写 commit 信息，例如：`chore: restructure project upload`
5. 点击 **Commit changes**

## 不要上传

- `python-portable/`（第三方运行时）
- `output/`、`data/` 中的密钥 / 数据库
- `*.log`、`__pycache__/`

## 克隆后安装

```bat
install.bat
```
""",
        encoding="utf-8",
    )

    # 6) zip 备份（可选拖 zip 不如拖文件夹，但留一份归档）
    zip_path = ROOT / "github_upload.zip"
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in OUT.rglob("*"):
            if p.is_file():
                zf.write(p, p.relative_to(OUT))
    print(f"\n完成：{copied} 个文件 → {OUT}")
    print(f"压缩包：{zip_path}")
    print("\n下一步：GitHub → Add file → Upload files → 拖入 github_upload 内全部内容")


if __name__ == "__main__":
    sys.exit(build() or 0)
