# data.tool U 盘便携版使用说明

## 特点

- 复制整个项目文件夹到 U 盘，在任何 Windows 10/11 电脑上双击 `一键启动.bat` 即可运行
- 无需在目标电脑安装 Python
- 自动绑定 `0.0.0.0:5000`，同一 WiFi 下其他设备可通过局域网地址访问

## 一、准备工作（在您自己电脑上做一次）

1. 确保本机可以联网
2. 双击运行 `setup_portable.bat`
   - 自动下载官方 Python 3.11 便携版（约 30MB）
   - 自动安装全部依赖（约 500MB，耗时 10~30 分钟）
   - 完成后项目里会多出一个 `python-portable\` 文件夹
3. 看到 `SETPL COMPLETE` 字样即成功

> 如果 `setup_portable.bat` 被 Windows SmartScreen 拦截：点击"更多信息" → "仍要运行"。

## 二、复制到 U 盘

将**整个项目文件夹**（包含 `python-portable\`）复制到 U 盘。

注意：`python-portable\` 约 400~700MB，请确认 U 盘空间足够。

建议复制前排除以下可选目录以减小体积（不影响运行）：
- `output\` 里的历史数据文件（保留空文件夹结构即可）
- `__pycache__\`
- `backup_优化前\`
- `.workbuddy\`、`.mimocode\`

## 三、在其他电脑上使用

1. 插入 U 盘，打开项目文件夹
2. 双击 `一键启动.bat`
3. 首次运行如弹出 Windows 防火墙提示 → 勾选"专用网络" → 点"允许访问"（其他设备才能通过 WiFi 访问）
4. 浏览器会自动打开 `http://127.0.0.1:5000`
5. 窗口中会显示局域网地址（如 `http://192.168.x.x:5000`），同一 WiFi 下的手机/平板/其他电脑可直接访问

停止服务：关闭命令行窗口，或运行 `stop.bat`。

## 四、常见问题

**Q: 双击后闪退？**
用命令行运行看报错：按 `Win+R` 输入 `cmd`，回车，把 `一键启动.bat` 拖进窗口，回车。

**Q: 目标电脑提示"Windows 已保护你的电脑"？**
这是 SmartScreen 对 U 盘脚本的正常拦截，点"更多信息" → "仍要运行"。

**Q: 端口被占用？**
`一键启动.bat` 会自动清理 5000 端口残留进程。若仍失败，先运行 `stop.bat`。

**Q: 能否放到网盘/共享文件夹？**
可以。只要目标电脑是 Windows 且能解压/访问该文件夹即可。

**Q: 不想带 python-portable，让目标电脑自己装？**
也可以：不复制 `python-portable\` 文件夹。目标电脑需已安装 Python 3.11+ 并加入 PATH，首次运行会自动创建 venv 并联网安装依赖（需要目标电脑能联网）。

## 五、文件说明

| 文件 | 作用 |
|---|---|
| `一键启动.bat` | 主入口：自动找 Python → 装依赖 → 起服务 |
| `setup_portable.bat` | 一次性准备：下载便携 Python + 依赖到 `python-portable\` |
| `start.bat` | 兼容入口（等价于一键启动.bat） |
| `stop.bat` | 停止服务并释放 5000 端口 |
| `python-portable\` | 便携 Python 运行时（setup_portable.bat 生成） |
| `launcher.py` | 可选：供外部程序以无窗口方式调起服务 |
