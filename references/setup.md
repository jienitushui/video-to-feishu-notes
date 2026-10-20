# 安装与飞书配置

## 环境

- Python 3.10+、Git，以及能执行本地命令的通用 Agent。
- 飞书自建应用及其可访问的多维表格、云盘文件夹。
- 飞书 CLI 依赖 requests；视频依赖从固定版本的上游单独安装。

仓库根目录就是 Skill 目录。可克隆到 Agent 的 skills 目录，例如：

    git clone https://github.com/jienitushui/video-to-feishu-notes.git ~/.agents/skills/video-to-feishu-notes

进入目录后创建虚拟环境：

    python -m venv .venv

Windows 使用 `.venv/Scripts/python.exe`，macOS/Linux 使用 `.venv/bin/python`。以下用 python 代表选定环境的解释器；在 Agent 中建议使用完整路径，上游工具也会使用同一解释器。

    python -m pip install -r requirements.txt

## 配置与授权

复制 config.example.json 为 config.json，填写：

- app_token：多维表格应用标识，来自 /base/<app_token> 链接，不是 API access token。知识库页面需先解析实际表格标识。
- table_id：任务数据表 ID。
- folder_token：目标云盘文件夹标识；首版要求明确配置。
- document_url_prefix：建议使用租户的 https://<租户域名>/docx/。
- upstream_path：视频工具目录，相对配置文件所在目录解析。
- fields：可选字段名映射，见 table-schema.md。

凭据使用环境变量，PowerShell 示例：

    $env:FEISHU_APP_ID = '<应用 ID>'
    $env:FEISHU_APP_SECRET = '<应用密钥>'

也支持 FEISHU_USER_ACCESS_TOKEN 或 FEISHU_TENANT_ACCESS_TOKEN。优先 user token，其次 tenant token，否则使用应用密钥换取 tenant token。直接配置的短期 token 过期后由用户或宿主更新，CLI 不保存或刷新 user token。

不要将真实密钥提交到 Git，或写入命令参数。config.json 已被忽略，仍应仅存放资源标识与非敏感设置。CLI 不自动读取 .env。

在飞书开放平台发布应用并配置所需权限。所需能力包括：读取及更新多维表格记录、读取及新增字段（仅初始化时）、创建/读取/编辑新版云文档、上传文档素材。具体 scope 名称以实际接口要求为准。目标表格与文件夹还需要实际协作访问权，开通 API 权限不等于可访问所有文件。

## 初始化表格

    python scripts/vfn.py doctor
    python scripts/vfn.py doctor --remote
    python scripts/vfn.py schema
    python scripts/vfn.py schema --apply

schema 默认只返回缺失字段；--apply 创建缺失字段，不删除已有字段。已有同名字段类型不兼容时停止，可配置 fields 映射。表格视图、隐藏列和展示顺序由用户按偏好调整。

API 回读只能确认当前调用身份可访问。首次发布后请用户实际打开文档，确认文件夹的成员权限满足阅读需求。工具不会自动公开分享。

## 视频处理工具

    python scripts/vfn.py video install-upstream
    python -m pip install -r cache/upstream/requirements.txt

默认固定提交 2497a7f564670c840f5e1c4055c8a536237cd5f3。目录已存在时停止，不覆盖。可配置已有上游目录，但版本须匹配，详见 upstream.md。首次下载 ASR 模型需要网络与磁盘空间；无字幕转写、截图才需要完整媒体依赖。

主 Skill 不自动登录平台，不自动删除媒体缓存。B站登录由用户按需执行上游 bili_fetch.py --login，授权信息保存在上游本地配置。

## 常用命令

    python scripts/vfn.py enqueue '<视频链接>'
    python scripts/vfn.py enqueue '<视频链接>' --enabled --requirements '重点解释代码' --category 'AI'
    python scripts/vfn.py tasks --limit 3 --category 'AI'
    python scripts/vfn.py claim <record_id>
    python scripts/vfn.py stage <record_id> --run-id <run_id> --stage '本地转写'
    python scripts/vfn.py video prepare '<表格中的完整原始输入>' --output output/<record_id>-v1 --transcribe --screenshots
    python scripts/vfn.py bind-source <record_id> --run-id <run_id> --manifest output/<record_id>-v1/manifest.json
    python scripts/vfn.py validate-note output/<record_id>-v1/note.json
    python scripts/vfn.py publish <record_id> --run-id <run_id> --note output/<record_id>-v1/note.json

prepare 的原始输入必须与任务一致。有些上游脚本出错仍返回零退出码，适配器还检查实际转写文件。命令成功输出 ok=true 和 data；失败输出 ok=false、error_type、error 并退出 1。doctor 的 ok=true 表示检查已执行，配置完整性应看具体检查项。

## MCP 适配

有飞书写入工具的 Agent 可按 table-schema.md 与 state-and-recovery.md 的协议直接操作；需要图片上传和文档回读能力。CLI 是默认实现，不假定任意 MCP 都支持同名操作。只有只读能力时应报告缺失的写入能力。
