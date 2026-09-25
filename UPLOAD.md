# 如何上传到 GitHub

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


## 网页拖不了文件夹时

在项目根目录双击 **upload_to_github_api.bat**（需 GitHub Token），可按完整目录结构推送到仓库。
