#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
data.tool 启动器
一键启动数据采集分析工具 Web 服务

用法:
    python launcher.py              # 默认端口 5000
    python launcher.py --port 8080  # 指定端口
    python launcher.py --no-browser # 不自动打开浏览器
"""

import os
import sys
import socket
import subprocess
import argparse
import webbrowser
import signal
import time
from pathlib import Path

# ==================== 配置 ====================
PROJECT_DIR = Path(__file__).parent.resolve()
PYTHON_EXE = sys.executable
DEFAULT_PORT = 8080


# ==================== 工具函数 ====================

def get_lan_ip() -> str:
    """获取局域网 IP"""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        try:
            return socket.gethostbyname(socket.gethostname())
        except Exception:
            return None


def is_port_in_use(port: int) -> bool:
    """检查端口是否被占用"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def kill_process_on_port(port: int):
    """终止占用指定端口的进程"""
    if not is_port_in_use(port):
        return
    
    # 先尝试优雅终止 (Windows)
    try:
        result = subprocess.run(
            ["netstat", "-ano"],
            capture_output=True, text=True, timeout=5
        )
        for line in result.stdout.splitlines():
            if f":{port}" in line and "LISTENING" in line:
                pid = line.strip().split()[-1]
                subprocess.run(["taskkill", "/F", "/PID", pid],
                             capture_output=True, timeout=5)
                time.sleep(0.5)
                return
    except Exception:
        pass


def check_dependencies() -> bool:
    """检查必要依赖"""
    required = ["flask", "pandas", "numpy"]
    missing = []
    
    for pkg in required:
        try:
            __import__(pkg)
        except ImportError:
            missing.append(pkg)
    
    if missing:
        print(f"⚠️  缺少依赖: {', '.join(missing)}")
        print(f"   请运行: pip install {' '.join(missing)}")
        return False
    return True


# ==================== 主程序 ====================

def main():
    parser = argparse.ArgumentParser(description="data.tool 启动器")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"端口号 (默认 {DEFAULT_PORT})")
    parser.add_argument("--host", default="0.0.0.0", help="绑定地址 (默认 0.0.0.0)")
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    parser.add_argument("--debug", action="store_true", help="启用调试模式")
    args = parser.parse_args()
    
    port = args.port
    host = args.host
    open_browser = not args.no_browser
    debug = args.debug
    
    # 打印横幅
    print("\n" + "=" * 55)
    print("  🚀 data.tool  数据采集分析工具")
    print("=" * 55)
    
    # 检查依赖
    if not check_dependencies():
        sys.exit(1)
    
    # 检查端口
    if is_port_in_use(port):
        print(f"⚠️  端口 {port} 已被占用，正在释放...")
        kill_process_on_port(port)
        time.sleep(1)
        if is_port_in_use(port):
            print(f"❌ 无法释放端口 {port}，请手动关闭占用进程")
            sys.exit(1)
        print(f"✅ 端口 {port} 已释放")
    
    # 获取地址
    local_url = f"http://127.0.0.1:{port}"
    lan_ip = get_lan_ip()
    lan_url = f"http://{lan_ip}:{port}" if lan_ip else None
    
    print(f"\n  📍 本机访问:  {local_url}")
    if lan_url:
        print(f"  🌐 局域网:    {lan_url}")
    print(f"  📂 项目目录:  {PROJECT_DIR}")
    print(f"  🐍 Python:    {PYTHON_EXE}")
    if debug:
        print(f"  🔧 调试模式:  开启")
    print("\n" + "=" * 55)
    print("  按 Ctrl+C 停止服务器\n")
    
    # 切换到项目目录
    os.chdir(PROJECT_DIR)
    
    # 自动打开浏览器
    if open_browser:
        try:
            webbrowser.open(local_url)
        except Exception:
            pass
    
    # 启动 Flask
    env = os.environ.copy()
    env["FLASK_DEBUG"] = "1" if debug else "0"
    env["FLASK_PORT"] = str(port)
    
    try:
        process = subprocess.run(
            [PYTHON_EXE, "app.py"],
            cwd=PROJECT_DIR,
            env=env
        )
        sys.exit(process.returncode)
    except KeyboardInterrupt:
        print("\n\n🛑 服务器已停止")
        sys.exit(0)
    except Exception as e:
        print(f"\n❌ 启动失败: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
