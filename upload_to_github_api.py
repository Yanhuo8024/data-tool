# -*- coding: utf-8 -*-
"""
upload_to_github_api.py — 推送 github_upload/ 到 GitHub（保留目录结构）

稳定性增强（2026-09-30）：
  · 连接被远端掐断 / 超时 → 指数退避重试
  · blob 上传写 progress.json，失败可续传
  · 自动 gzip 请求体，减小体积
"""
from __future__ import annotations

import base64
import gzip
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "github_upload"
TOKEN_FILE = ROOT / "github_token.txt"
PROGRESS = ROOT / ".workbuddy_upload_progress.json"
API = "https://api.github.com"
DEFAULT_REPO = os.environ.get("GITHUB_REPO") or "Yanhuo8024/data-tool"
BRANCH = os.environ.get("GITHUB_BRANCH") or "main"
RETRIES = 6


def _get_token() -> str:
    if TOKEN_FILE.is_file():
        t = TOKEN_FILE.read_text(encoding="utf-8").strip().strip('"').strip("'")
        if t:
            print("Token: github_token.txt")
            return t
    t = (os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or "").strip()
    if t:
        print("Token: env")
        return t
    print("Paste GitHub token (right-click paste), then Enter:")
    try:
        t = input("TOKEN> ").strip()
    except Exception:
        t = ""
    return t.strip().strip('"').strip("'")


def _req(method: str, url: str, token: str, payload=None):
    data = None
    gzip_on = False
    if payload is not None:
        # GitHub Git Data API 不接受 gzip JSON，必须明文 body
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        data = raw
    last = None
    for attempt in range(RETRIES):
        r = urllib.request.Request(url, data=data, method=method)
        r.add_header("Authorization", f"Bearer {token}")
        r.add_header("Accept", "application/vnd.github+json")
        r.add_header("X-GitHub-Api-Version", "2022-11-28")
        r.add_header("User-Agent", "data-tool-uploader")
        r.add_header("Connection", "close")
        if data is not None:
            # 必须带 charset，否则含中文的 UTF-8 body 会被当成 latin-1 → GitHub "Problems parsing JSON"
            r.add_header("Content-Type", "application/json; charset=utf-8")
            if gzip_on:
                r.add_header("Content-Encoding", "gzip")
        try:
            with urllib.request.urlopen(r, timeout=90) as resp:
                body = resp.read().decode("utf-8")
                return json.loads(body) if body else {}
        except urllib.error.HTTPError as e:
            err = e.read().decode("utf-8", errors="replace")
            if e.code in (403, 429) and attempt < RETRIES - 1:
                wait = 2 ** attempt
                print(f"  rate-limited {e.code}, sleep {wait}s")
                time.sleep(wait)
                continue
            raise RuntimeError(f"HTTP {e.code} {method} {url}\n{err[:500]}") from e
        except Exception as e:
            last = e
            wait = min(12, 1.2 * (2 ** attempt))
            print(f"  retry {attempt + 1}/{RETRIES} after {type(e).__name__}: {e} (sleep {wait:.1f}s)")
            time.sleep(wait)
    raise RuntimeError(f"network failed after {RETRIES} retries: {last}")


def _load_progress():
    try:
        return json.loads(PROGRESS.read_text(encoding="utf-8"))
    except Exception:
        return {"blobs": {}}


def _save_progress(p):
    try:
        PROGRESS.write_text(json.dumps(p, ensure_ascii=False), encoding="utf-8")
    except Exception as _e:
        logger.debug("upload_to_github_api.py:100 swallowed: %s", _e)


def _upload_blob(token, repo, path: Path, progress: dict) -> str:
    key = str(path.relative_to(SRC).as_posix())
    sha = (progress.get("blobs") or {}).get(key)
    if sha:
        return sha
    raw = path.read_bytes()
    # 统一 base64：GitHub Git Blobs 对 utf-8 文本 + 特殊字符/空字节/非法序列更挑剔，
    # base64 是唯一稳定路径（避免 "Problems parsing JSON"）。
    content = base64.b64encode(raw).decode("ascii")
    payload = {"content": content, "encoding": "base64"}
    blob = _req("POST", f"{API}/repos/{repo}/git/blobs", token, payload)
    sha = blob.get("sha") or ""
    if len(sha) != 40:
        raise RuntimeError(f"bad blob sha {sha!r}")
    progress.setdefault("blobs", {})[key] = sha
    if len(progress["blobs"]) % 20 == 0:
        _save_progress(progress)
    return sha


def _create_tree(token, repo, entries, base_tree=None):
    payload = {"tree": entries}
    if base_tree:
        payload["base_tree"] = base_tree
    return _req("POST", f"{API}/repos/{repo}/git/trees", token, payload)


def build_bottom_up(token, repo, root: Path, progress: dict) -> str:
    files = [p for p in root.rglob("*") if p.is_file()]
    print(f"blobs: {len(files)}")
    path_sha = {}
    for i, p in enumerate(files, 1):
        rel = p.relative_to(root).as_posix()
        path_sha[rel] = _upload_blob(token, repo, p, progress)
        if i % 25 == 0 or i == len(files):
            print(f"  blob {i}/{len(files)}")
    _save_progress(progress)

    from collections import defaultdict
    dir_files = defaultdict(list)
    for rel, sha in path_sha.items():
        parent, name = rel.rsplit("/", 1) if "/" in rel else ("", rel)
        dir_files[parent].append((name, sha))

    dirs = set(dir_files.keys())
    for d in list(dirs):
        while d:
            parent = d.rsplit("/", 1)[0] if "/" in d else ""
            dirs.add(parent)
            d = parent

    def depth(d):
        return (d.count("/") + 1) if d else 0

    ordered = sorted(dirs, key=depth, reverse=True)
    tree_sha = {}
    for d in ordered:
        entries = []
        for name, sha in sorted(dir_files.get(d, []), key=lambda x: x[0]):
            entries.append({"path": name, "mode": "100644", "type": "blob", "sha": sha})
        prefix = (d + "/") if d else ""
        child_dirs = set()
        for od in dirs:
            if od == d:
                continue
            if d == "":
                if "/" not in od:
                    child_dirs.add(od)
            elif od.startswith(prefix) and "/" not in od[len(prefix):]:
                child_dirs.add(od)
        for cd in sorted(child_dirs):
            entries.append({"path": cd, "mode": "040000", "type": "tree", "sha": tree_sha[cd]})
        tree = _create_tree(token, repo, entries)
        tree_sha[d] = tree["sha"]
        print(f"  tree {d or '(root)'} {len(entries)} entries")
    return tree_sha[""]


def main() -> int:
    token = _get_token()
    if not token:
        print("ERROR: empty token")
        return 1
    if not SRC.is_dir():
        print("ERROR: missing github_upload")
        print("Run first: python prepare_github_upload.py")
        return 1

    repo = DEFAULT_REPO
    print("Repo:", repo)
    info = _req("GET", f"{API}/repos/{repo}", token)
    branch = info.get("default_branch") or BRANCH
    print("Branch:", branch)

    base_sha = None
    try:
        ref = _req("GET", f"{API}/repos/{repo}/git/ref/heads/{branch}", token)
        base_sha = ref["object"]["sha"]
        print("HEAD:", base_sha[:12])
    except RuntimeError as e:
        if "404" not in str(e):
            raise
        print("No branch yet")

    progress = _load_progress()
    print("cached blobs:", len(progress.get("blobs") or {}))
    root_tree = build_bottom_up(token, repo, SRC, progress)
    _save_progress(progress)

    print("Creating commit...")
    commit = _req("POST", f"{API}/repos/{repo}/git/commits", token, {
        "message": "chore: sync project with full folder structure",
        "tree": root_tree,
        "parents": [base_sha] if base_sha else [],
    })

    if base_sha:
        _req("PATCH", f"{API}/repos/{repo}/git/refs/heads/{branch}", token,
             {"sha": commit["sha"]})
    else:
        _req("POST", f"{API}/repos/{repo}/git/refs", token,
             {"ref": f"refs/heads/{branch}", "sha": commit["sha"]})

    print("SUCCESS")
    print("commit:", commit["sha"][:12])
    print("url: https://github.com/" + repo + "/tree/" + branch)
    try:
        PROGRESS.unlink()
    except Exception as _e:
        logger.debug("progress unlink swallowed: %s", _e)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print("FAILED:", e)
        sys.exit(1)