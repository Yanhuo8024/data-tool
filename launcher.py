"""
data.tool launcher - portable / USB friendly
Launches 一键启动.bat from the same directory as this script.
"""
import os
import subprocess
import sys

current_dir = os.path.dirname(os.path.abspath(__file__))
bat_path = os.path.join(current_dir, "一键启动.bat")

if not os.path.exists(bat_path):
    # Fallback for environments where the Chinese filename is mangled
    bat_path = os.path.join(current_dir, "start.bat")

try:
    subprocess.Popen(
        [bat_path], shell=True, cwd=current_dir,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )
except Exception as e:
    print(f"启动失败: {e}")
    input("按回车键退出...")
