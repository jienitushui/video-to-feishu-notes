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

在飞书开放平台发布应用并配置所需权限。所需能力包括：读取及更新多维表格记录、读取及新增字段（仅初始化时）、创建/读取/编辑新版云文档、上传文档素材；wiki/both 模式另需读取知识库节点和创建子节点，folder/both 模式的远程检查需要列举文件夹内容。默认只留最新版还需要删除旧文档能力：文件删除接口要求 `space:document:delete` 或 `drive:drive` 之一，以及文件所有者与父目录编辑权限，或父目录所有者/完全管理权限。删除进入飞书回收站，接口按串行调用；权限不足时保留清理队列，待修复后重试。具体 scope 名称以实际接口要求为准。目标表格、知识库目录或文件夹还需要调用身份的实际协作访问权，开通 API 权限不等于可访问所有文件。

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

工具直接调用知识库创建 origin/docx 子节点，取得 obj_token 后使用文档接口写入正文和图片，最终回填 /wiki/<node_token> 链接。不需要先创建云盘文档再移动。创建时草稿即出现在知识库，表格显示完成后才代表内容已校验；新版本发布、图片校验和表格链接回读全部成功后，再清理旧页面。旧页面有子页面或关联位置改变时停止清理并提示。

若使用云盘，设置 publish_target 为 folder 并填写 folder_token 即可。更改目标后，已完成任务默认仍跳过；需要在新目标重新生成的记录须明确请求重新生成。新任务只复用同一目标目录中的笔记。旧版本缺少归档元数据的结果不参与自动复用；旧云盘草稿可继续以 folder 模式恢复。

## 知识库主文档与云盘生成备份

将 publish_target 设为 both，并同时填写 folder_token、wiki_space_id、wiki_parent_node_token。笔记只生成一次：知识库文档作为日常阅读和编辑的主文档，云盘文档作为生成时的备份。发布工具先写云盘备份，再写知识库主文档；两份有独立的正文 ID 和访问权限，正文与图片来自同一 note.json。新云盘副本的标题标注“生成备份”，仅归档图文笔记，不上传原视频和完整中间素材。

完成后主文档的手动修改不会更新或覆盖生成备份，已完成任务默认跳过。需要重新生成时创建新主文档和新备份；新版发布、正文与图片校验、表格链接更新并回读确认全部成功后，自动删除旧主文档及旧备份，默认仅留最新版。新版未全部成功前保留旧版，清理失败用原 run_id、note.json 和图片执行 `cleanup-versions` 单独重试，不重做发布。删除前逐张下载新版图片核对 SHA-256，需具备素材下载权限（例如 `docs:document.media:download` 或 `drive:drive:readonly`）及实际资源访问权；下载失败也会保留旧版。只清理检查点中明确记录的旧成功版本，不追溯已有历史文档，具体保护规则见 [状态与恢复](state-and-recovery.md)。这不能代替对手动修改的备份。当前 CLI 没有“备份编辑后的主文档”命令，不能将重新生成视频笔记当作备份手动修改。

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

### 主列与空白行

飞书新建多维表格时会自动创建一个名为“文本”的主列，并可能预留几条空白记录。建议将这个主列重命名为“任务名称”，并在 `config.json` 中保留映射：

```json
"fields": {
  "task_name": "任务名称"
}
```

项目将这列映射为 `task_name`：`enqueue` 创建任务时先写入分享文本，`bind-source` 成功后更新为视频标题。这样第一列不会长期为空；前五行等历史空记录不会被 `schema --apply` 自动删除。

清理历史空记录前先预览：

```powershell
.\.venv\Scripts\vfn.exe --env-file .env.local --config config.json cleanup-empty
```

确认候选记录确实为空后，再加 `--apply` 删除。该命令只匹配所有字段都为空的记录，不碰已提交、处理中或已完成的任务；“文本”列仍可在飞书界面手动改名，改名后在 `config.json` 中配置 `fields.task_name` 与新名称。

doctor --remote 只读检查表格字段及目标目录：wiki 模式校验父节点所属知识空间，folder 模式列举文件夹，both 模式分别检查两处。读取成功不代表有创建权限，报告中 write_access_checked 为 false；首次实际发布再验证写入。

API 回读只能确认当前调用身份可访问。首次发布后请用户实际打开文档，确认知识库或文件夹的成员权限满足阅读需求。工具不会自动公开分享。

## 常见权限问题与排障记录

本节记录一次真实联调中遇到的两个权限问题。它们通常不是代码或 `config.json` 写错，而是应用版本、资源协作权限和 API 权限没有同时生效。

### 云盘文件夹返回 `1061004: forbidden`

本项目采用应用身份直连资源，不需要创建群、不需要启用机器人，也不需要把机器人加入群。打开目标云盘文件夹，选择「分享」→「邀请协作者」，直接搜索“视频笔记助手”应用并授予**可管理**。然后在「权限管理」确认已开通读取云空间文件夹清单，以及创建/编辑文档、上传素材所需权限；修改权限后，在「版本管理与发布」创建并**正式发布**新版本。

如果分享窗口无法搜索到应用，先检查应用类型和租户是否允许文档应用直授权；不要把“机器人入群”当作本项目的默认依赖。当前目标租户已验证应用可以直接加入文件夹，项目群权限已移除。

检查命令：

```powershell
.\.venv\Scripts\vfn.exe --env-file .env.local --config config.json doctor --remote
```

恢复后应看到 `destination.readable: true`。`write_access_checked: false` 是只读检查的正常结果，首次 `publish` 才会验证写权限。

### `schema --apply` 返回 `91403: Forbidden`

1. 在「权限管理」添加多维表格新增字段权限，权限标识通常为 `base:field:create`，然后发布新版本。
2. 打开目标多维表格，右上角「分享」→「邀请协作者」，直接搜索当前自建应用/文档应用，权限设为**可管理**并发送邀请；不需要机器人入群。
3. 重新运行：

```powershell
.\.venv\Scripts\vfn.exe --env-file .env.local --config config.json schema --apply
```

成功时 `missing` 必须为空；`created: 0` 也可能是成功，表示字段已经存在。命令只补字段，不删除旧字段、不改视图、不迁移历史链接。

### 权限排查顺序

飞书权限有三层：开放平台 API 权限、已正式发布的应用版本、目标文件夹/知识库/多维表格中的资源协作权限。缺一层都可能返回 `Forbidden`。确认三层后，再运行 `schema` 和 `doctor --remote`。

本次联调的最终状态：应用版本 `1.0.2` 已发布；多维表格、知识库父页面和云盘文件夹均已直接将“视频笔记助手”应用设为可管理；`schema --apply` 的 `missing` 为空；项目群权限已移除，`doctor --remote` 仍显示两个目标 `readable: true`，并已成功完成知识库主文档 + 云盘备份双归档。这证明当前租户可以完全不创建项目群运行本项目。

### 直连模式的最终配置

CLI 使用 `.env.local` 中的 `FEISHU_APP_ID` 和 `FEISHU_APP_SECRET` 换取租户令牌，所有 API 请求直接由自建应用身份发出。群不会转发接口请求，也不需要承接云盘权限。

需要满足三项：

- 开放平台已正式发布包含多维表格、知识库、云盘文件和文档读写能力的应用版本；
- 应用本身已被直接加入目标多维表格、知识库父页面和云盘文件夹，并授予“可管理”；
- `config.json` 填写 `app_token`、`table_id`、`wiki_space_id`、`wiki_parent_node_token` 和 `folder_token`，`publish_target` 使用 `both`。

验证命令：

```powershell
.\\.venv\\Scripts\\vfn.exe --env-file .env.local --config config.json doctor --remote
```

看到 `destination.type` 为 `both`，且 folder/wiki 两个目标均为 `readable: true`，就可以直接运行项目；全程不需要群，也不需要机器人加入群。

### 插图返回 `1770013: relation mismatch`

新版文档的图片上传点必须是图片块的 `block_id`，不能使用文档的 `document_id`。调用 `/drive/v1/medias/upload_all` 时设置 `parent_type=docx_image`、`parent_node=<图片块ID>`，并在 `extra` 中传入 `{"drive_route_token":"<文档ID>"}`，然后将返回的素材 token 写入该图片块。参见[飞书素材概述](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/drive-v1/media/introduction)。

遇到该错误时保留检查点，升级修正后的代码，再使用相同 `run_id` 和未改动的 `note.json` 续发；发布器会回读已有块，继续填充空图片块，无需重新创建文档。

## 视频处理工具

    python scripts/vfn.py video install-upstream
    python -m pip install -r cache/upstream/requirements.txt

默认固定提交 2497a7f564670c840f5e1c4055c8a536237cd5f3。目录已存在时停止，不覆盖。可配置已有上游目录，但版本须匹配，详见 upstream.md。首次下载 ASR 模型需要网络与磁盘空间；无字幕转写、截图才需要完整媒体依赖。

抖音额外说明：固定版本的上游抖音脚本需要使用者自行获取 GPLv3 的 `abogus.py`，项目不会把它打包进仓库。运行抖音流程时若提示缺少该文件，请将固定 commit 对应文件放到 `cache/upstream/scripts/abogus.py`；脚本会校验 SHA-256、关键标记和 Python 编译结果。抖音签名还需要 `gmssl`：

    python -m pip install gmssl

若启用无字幕本地转写和截图，还需要上游 requirements 中的 `faster-whisper`、`av`、`numpy`、`pillow`。本次联调还发现固定上游脚本与最新 PyAV 不兼容：出现 `open() got an unexpected keyword argument 'metadata_errors'` 时，将 PyAV 调整到兼容版本，例如：

    python -m pip install --force-reinstall --only-binary=:all: av==12.3.0

安装后可用 `doctor` 检查 `optional_modules`，再重新运行 `video prepare --transcribe --screenshots`。

主 Skill 不自动登录平台，不自动删除媒体缓存。B站登录由用户按需执行上游 bili_fetch.py --login，授权信息保存在上游本地配置。

## 常用命令

    python scripts/vfn.py enqueue '<视频链接>'
    python scripts/vfn.py enqueue '<视频链接>' --enabled --requirements '重点解释代码' --category 'AI'
    python scripts/vfn.py tasks --limit 3 --category 'AI'
    python scripts/vfn.py claim <record_id>
    python scripts/vfn.py stage <record_id> --run-id <run_id> --stage '本地转写'
    python scripts/vfn.py video prepare '<表格中的完整原始输入>' --record-id <record_id> --version <生成版本> --transcribe --screenshots
    python scripts/vfn.py bind-source <record_id> --run-id <run_id> --manifest output/<record_id>/v<生成版本>/manifest.json
    python scripts/vfn.py validate-note output/<record_id>/v<生成版本>/note.json
    python scripts/vfn.py publish <record_id> --run-id <run_id> --note output/<record_id>/v<生成版本>/note.json

prepare 的原始输入必须与任务一致。有些上游脚本出错仍返回零退出码，适配器还检查实际转写文件。命令成功输出 ok=true 和 data；失败输出 ok=false、error_type、error 并退出 1。doctor 的 ok=true 表示检查已执行，配置完整性应看具体检查项。

ASR 参数：`asr_language` 默认 `auto`；`asr_hotwords` 为可选术语字符串数组，独立于笔记展示语言 `language`。命令行 `--asr-language` 覆盖语言，重复 `--hotword` 追加术语；续跑保持设置一致。短视频整段、长视频分片均传入这些设置。原稿、校正版留存与 `video finalize-transcript` 使用见 [transcripts.md](transcripts.md)。这些本地命令不发布飞书文档。

## MCP 适配

有飞书写入工具的 Agent 可按 table-schema.md 与 state-and-recovery.md 的协议直接操作；需要图片上传和文档回读能力。CLI 是默认实现，不假定任意 MCP 都支持同名操作。只有只读能力时应报告缺失的写入能力。
