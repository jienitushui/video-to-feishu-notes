# 安装与飞书配置

## 环境

- Python 3.10+、Git，以及能执行本地命令的通用 Agent。
- 飞书自建应用及其可访问的多维表格、目标知识库目录或云盘文件夹。
- 飞书 CLI 依赖 requests 和 python-dotenv；视频依赖从固定版本的上游单独安装。

仓库根目录就是 Skill 目录。可克隆到 Agent 的 skills 目录，例如：

    git clone https://github.com/jienitushui/video-to-feishu-notes.git ~/.agents/skills/video-to-feishu-notes

进入目录后创建虚拟环境：

    python -m venv .venv

Windows 使用 `.venv/Scripts/python.exe`，macOS/Linux 使用 `.venv/bin/python`。以下用 python 代表选定环境的解释器；在 Agent 中建议使用完整路径，上游工具也会使用同一解释器。

    python -m pip install -r requirements.txt

## 安装 vfn 短命令

在仓库根目录用虚拟环境执行可编辑安装。入口固定使用安装时的 Python，读取当前 checkout 的代码；移动仓库或虚拟环境后需重新安装。

Windows PowerShell：

```powershell
.venv/Scripts/python.exe -m pip install -e .
# 将入口目录加入当前终端的 PATH，无需执行激活脚本
$vfnScripts = (Resolve-Path .venv/Scripts).Path
$env:Path = "$vfnScripts;$env:Path"
vfn --help
```

macOS / Linux（bash、zsh）：

```bash
.venv/bin/python -m pip install -e .
source .venv/bin/activate
vfn --help
```

pip 在 Windows 生成 `.venv/Scripts/vfn.exe`，在 macOS/Linux 生成 `.venv/bin/vfn`。新终端需重新激活虚拟环境或设置 PATH；若需要长期直接调用，可将该虚拟环境的入口目录加入用户 PATH。也可始终用入口完整路径，无需管理员权限或修改系统目录。

`vfn` 与 `python scripts/vfn.py` 使用同一实现，参数和退出码一致，不改变运行目录；`--output`、笔记路径等相对路径按调用者当前目录解析。默认配置仍来自项目根目录的 `config.json`，其中 upstream_path 等路径按配置文件目录解析。

## 配置与授权

复制 config.example.json 为 config.json，填写：

- app_token：多维表格应用标识，来自 /base/<app_token> 链接，不是 API access token。知识库页面需先解析实际表格标识。
- table_id：任务数据表 ID。
- publish_target：folder（仅云盘）、wiki（仅知识库）或 both（知识库主文档 + 云盘生成备份，示例默认）。旧配置未填写时按 folder 处理。
- wiki_space_id、wiki_parent_node_token：wiki/both 模式必填，分别为知识空间 ID 和归档父页面的节点 token。仅 wiki 模式无需 folder_token。
- wiki_url_prefix：wiki/both 模式回填链接前缀，建议使用租户的 https://<租户域名>/wiki/。
- folder_token：folder/both 模式必填，为目标云盘文件夹标识。
- document_url_prefix：建议使用租户的 https://<租户域名>/docx/。
- upstream_path：视频工具目录，相对配置文件所在目录解析。
- fields：可选字段名映射，见 table-schema.md。

### 获取云盘文件夹 token

`folder_token` 必须对应一个具体的云盘文件夹。云盘首页（例如“我的文件夹”根目录）不是目标文件夹；首页地址如 `/drive/me/` 不包含这里需要的文件夹 token。

1. 在云盘选择“新建文件夹”，创建用于保存生成备份的文件夹。
2. 打开新文件夹，复制浏览器地址栏中的链接；也可以从文件夹菜单选择“复制链接”。
3. 从该文件夹链接中确认并提取文件夹标识，填写到 `config.json` 的 `folder_token`。

飞书不同页面或租户的链接格式可能不同，不要仅凭固定的 `/folder/` 路径猜 token。如果链接中看不出标识，可把具体文件夹链接交给配置协助者核对；链接应指向刚创建的文件夹，而不是云盘首页。

凭据可保存到项目根目录的 `.env.local`，先复制 `.env.example`，再填写值：

```dotenv
FEISHU_APP_ID=cli_你的应用ID
FEISHU_APP_SECRET="你的应用密钥"
```

macOS/Linux 在填写真实密钥前运行 `chmod 600 .env.local`，仅允许文件所有者读写。Windows 使用文件安全属性限制访问，不使用 Unix 的 600 权限表示法。`.env.local` 已被 Git 忽略；仓库只包含无真实凭据的 `.env.example`。

也可继续手动设置环境变量，PowerShell 示例：

    $env:FEISHU_APP_ID = '<应用 ID>'
    $env:FEISHU_APP_SECRET = '<应用密钥>'

也支持 FEISHU_USER_ACCESS_TOKEN 或 FEISHU_TENANT_ACCESS_TOKEN。优先 user token，其次 tenant token，否则使用应用密钥换取 tenant token。直接配置的短期 token 过期后由用户或宿主更新，CLI 不保存或刷新 user token。

两个命令入口都在执行前统一加载凭据：

- 文件选择：`--env-file <路径>` 优先，其次环境变量 `VFN_ENV_FILE`，最后是项目根目录的 `.env.local`，不向上搜索，也不读取运行目录的同名文件。一次只读一个文件。
- 默认文件不存在时继续使用已有环境变量；显式指定的文件不存在时返回错误。
- 已存在的环境变量优先，包括空值；要改用文件值，先移除对应环境变量。
- 只读取上述四个 `FEISHU_*` 认证变量，不加载 PATH 或其他设置。文件支持 UTF-8（含 BOM）、注释和引号；不执行 shell 命令，不展开 `${变量}`。
- `--env-file` 和 `--config` 都放在子命令前，例如 `vfn --env-file /path/to/account.env --config /path/to/config.json doctor`。显式相对路径按运行目录解析，支持 `~` 凭据路径。

不要将真实密钥提交到 Git，或写入命令参数。config.json 已被忽略，仍只存放资源标识与非敏感设置；凭据文件不替代 config.json。CLI 不自动读取 `.env`。B站 SESSDATA 仍由上游本地配置管理。运行 `vfn doctor` 可查看 auth_configured 是否为 true；它只检查凭据是否已配置，不校验凭据有效性，也不输出凭据值。

在飞书开放平台发布应用并配置所需权限。所需能力包括：读取及更新多维表格记录、读取及新增字段（仅初始化时）、创建/读取/编辑新版云文档、上传文档素材；wiki/both 模式另需读取知识库节点和创建子节点，folder/both 模式的远程检查需要列举文件夹内容。具体 scope 名称以实际接口要求为准。目标表格、知识库目录或文件夹还需要调用身份的实际协作访问权，开通 API 权限不等于可访问所有文件。

## 选择知识库目录

在知识库创建或选择一个“视频学习笔记”父页面，后续每篇笔记作为它的子页面。父页面链接一般形如 https://<租户域名>/wiki/<node_token>；取路径中的 token，不能把它当作正文文档 ID 或知识空间 ID。

先复制 config.example.json 为 config.json，设置凭据后可运行只读解析（此命令不要求其他资源 ID 已填完）：

    python scripts/vfn.py wiki-node <父页面node_token>

从返回的 node 读取 space_id 和 node_token，填入 wiki_space_id 与 wiki_parent_node_token。space_id 建议以 JSON 字符串保存，避免长整数精度损失。也可用 wiki-node <文档ID> --obj-type docx 解析已归档文档的节点。

示例（仅展示归档部分，需与表格、视频等配置合并）：

```json
{
  "publish_target": "wiki",
  "wiki_space_id": "1234567890123456789",
  "wiki_parent_node_token": "父页面的节点token",
  "wiki_url_prefix": "https://你的租户.feishu.cn/wiki/"
}
```

工具直接调用知识库创建 origin/docx 子节点，取得 obj_token 后使用文档接口写入正文和图片，最终回填 /wiki/<node_token> 链接。不需要先创建云盘文档再移动。创建时草稿即出现在知识库，表格显示完成后才代表内容已校验；新版本保留旧页面。

若使用云盘，设置 publish_target 为 folder 并填写 folder_token 即可。更改目标后，已完成任务默认仍跳过；需要在新目标重新生成的记录须明确请求重新生成。新任务只复用同一目标目录中的笔记。旧版本缺少归档元数据的结果不参与自动复用；旧云盘草稿可继续以 folder 模式恢复。

## 知识库主文档与云盘生成备份

将 publish_target 设为 both，并同时填写 folder_token、wiki_space_id、wiki_parent_node_token。笔记只生成一次：知识库文档作为日常阅读和编辑的主文档，云盘文档作为生成时的备份。发布工具先写云盘备份，再写知识库主文档；两份有独立的正文 ID 和访问权限，正文与图片来自同一 note.json。新云盘副本的标题标注“生成备份”，仅归档图文笔记，不上传原视频和完整中间素材。

完成后主文档的手动修改不会更新或覆盖生成备份，已完成任务默认跳过。需要重新生成时创建新主文档和新备份，旧文档保留；这不能代替对手动修改的备份。当前 CLI 没有“备份编辑后的主文档”命令，不能将重新生成视频笔记当作备份手动修改。

```json
{
  "publish_target": "both",
  "folder_token": "云盘文件夹token",
  "wiki_space_id": "1234567890123456789",
  "wiki_parent_node_token": "知识库父页面token",
  "document_url_prefix": "https://你的租户.feishu.cn/docx/",
  "wiki_url_prefix": "https://你的租户.feishu.cn/wiki/"
}
```

表格使用两个超链接字段：“笔记文档”指向知识库主文档，“备份文档”指向云盘生成备份。两处都成功并回读验证后，才统一回填并标记已完成。仅云盘/仅知识库模式将对应文档写入“笔记文档”，“备份文档”清空，避免把主文档自身当作备份。新版本未完成前保留上次成功的两个链接；部分成功可从 show 的任务检查点和“当前阶段”查看。

旧表迁移：将原“云盘笔记”重命名为“备份文档”，保留其中已有链接；原“笔记文档”已指向知识库，可继续使用，原“知识库笔记”可隐藏。执行 schema --apply 补齐缺失字段；此命令不重命名、删除字段，也不回填历史记录链接。不方便改字段名时可配置 `"fields": {"folder_document": "云盘笔记"}`，新结果的备份仍回填原列。若还需维护旧“知识库笔记”列，可额外映射 `"wiki_document": "知识库笔记"`；只有显式配置时才检查并更新该兼容列，新表默认不创建它。上述映射需与已有 fields 合并。

一处失败不回滚已完成文档；修复权限或网络后明确重试，工具校验已完成副本，只继续缺失部分。两份都可访问且归档目标完全相同时才允许整体复用。现有单归档已完成结果不会因改为 both 自动补一份，需要明确重新生成；同一代中途改模式或目录会停止。

## 初始化表格

    python scripts/vfn.py doctor
    python scripts/vfn.py doctor --remote
    python scripts/vfn.py schema
    python scripts/vfn.py schema --apply

schema 默认只返回缺失字段；--apply 创建缺失字段，不删除已有字段。已有同名字段类型不兼容时停止，可配置 fields 映射。表格视图、隐藏列和展示顺序由用户按偏好调整。

doctor --remote 只读检查表格字段及目标目录：wiki 模式校验父节点所属知识空间，folder 模式列举文件夹，both 模式分别检查两处。读取成功不代表有创建权限，报告中 write_access_checked 为 false；首次实际发布再验证写入。

API 回读只能确认当前调用身份可访问。首次发布后请用户实际打开文档，确认知识库或文件夹的成员权限满足阅读需求。工具不会自动公开分享。

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
