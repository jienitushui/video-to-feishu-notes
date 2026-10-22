# video-to-feishu-notes

将 B站、抖音教程整理成图文课堂笔记，通过飞书多维表格管理任务，沉淀到飞书文档。

**当前状态：首版实现，可进行配置与联调。** 已包含 Skill、飞书 API 适配、视频工具适配和离线测试；支持仅云盘、仅知识库、同时归档两处三种模式，真实飞书端到端验证需要配置应用授权、任务表与归档目标。

## 设计思路

用户在飞书多维表格收集视频链接，再向支持 Skill 的通用 Agent 发起请求：

> 处理视频收集表中的待整理视频，最多 3 个。

Agent 按 Skill 读取任务、准备素材、生成笔记、写入飞书文档，并回填状态和文档链接。飞书保存任务状态，无需独立常驻后台服务。

```mermaid
flowchart LR
    A[用户启动通用 Agent] --> B[飞书表格读取任务]
    B --> C[字幕或本地转写]
    C --> D[截图与结构化笔记]
    D --> E[飞书文档]
    E --> F[回填任务状态和链接]
```

## 第一版能力

- 支持 B站单条视频或单个分 P，以及抖音视频。
- 字幕优先，无字幕时使用本地语音识别。
- 提取重点画面，生成章节笔记、知识地图、概念卡片和自测题。
- 将笔记与图片写入飞书文档；默认在知识库保存主文档、在云盘文件夹保存生成时的备份副本，也支持仅云盘或仅知识库。
- 支持去重、重新生成、有限重试和中断恢复。
- 由用户主动启动，单执行者串行处理。

## 安装与开始

仓库根目录即 Skill，可克隆到支持 Agent Skills 的工具目录：

```bash
git clone https://github.com/jienitushui/video-to-feishu-notes.git ~/.agents/skills/video-to-feishu-notes
cd ~/.agents/skills/video-to-feishu-notes
python -m venv .venv
```

使用 Python 3.10+。Windows 后续使用 `.venv/Scripts/python.exe`，macOS/Linux 使用 `.venv/bin/python`；以下 python 指该环境的解释器。

```bash
python -m pip install -r requirements.txt
# 复制 config.example.json 为 config.json，填写表格与归档目标
# publish_target: folder（仅云盘）/ wiki（仅知识库）/ both（知识库主文档 + 云盘生成备份，示例默认）
python scripts/vfn.py doctor
python scripts/vfn.py schema
python scripts/vfn.py schema --apply
python scripts/vfn.py video install-upstream
python -m pip install -r cache/upstream/requirements.txt
```

支持 Windows、macOS、Linux 的 `vfn` 短命令：在仓库根目录用虚拟环境运行 `python -m pip install -e .`，将该环境的入口目录加入 PATH（或激活环境），即可运行 `vfn doctor`。原有 Python 命令仍可使用。

复制 `.env.example` 为 `.env.local` 并填写飞书凭据；两个入口都会自动加载，已有环境变量优先。macOS/Linux 填写前设置 `chmod 600 .env.local`，Windows 用文件访问权限限制读取。凭据不写进 config.json 或提交到 Git。完整配置步骤、所需工具能力和命令参见 [安装配置](references/setup.md)。配置好后，对 Agent 说：

> 使用 video-to-feishu-notes，处理飞书视频收集表中的待整理任务，最多 3 个。

Agent 阅读 [SKILL.md](SKILL.md) 并调用辅助脚本，笔记由当前 Agent 生成，脚本无需额外的模型 API。

默认 both 模式需同时填写 folder_token、wiki_space_id 和 wiki_parent_node_token。视频只整理一次，生成两份独立在线文档：知识库供日常阅读和编辑，云盘保留最初生成的副本，标题标注“生成备份”。多维表格的“笔记文档”指向知识库，“备份文档”指向云盘。两份都验证成功后才标记完成；一处失败保留另一处进度，重试从检查点继续。

完成后手动修改知识库主文档不会改动云盘备份。重新生成会创建新主文档和新备份，保留旧文档，并在全部成功后更新表格链接。这是生成时的内容备份，不能恢复尚未备份的后续手动修改；当前没有“备份编辑后的主文档”命令。

已有表格将“云盘笔记”重命名为“备份文档”即可保留原链接，“知识库笔记”可隐藏；运行 schema --apply 补齐缺失字段。该命令只补字段，不迁移旧记录链接。原字段名需保留时，可用 fields 映射，见 [安装配置](references/setup.md)。

## 验证

```bash
python -m pip install -r requirements-dev.txt
python -m unittest discover -s tests -v
python scripts/vfn.py validate-note examples/note.json
```

离线测试覆盖重复视频复用、输入变化保护、旧版本保留、追加响应丢失、创建结果未知和文档完成后回填失败，以及知识库节点创建/恢复、目录变更保护与节点归属校验；双归档覆盖部分失败恢复、两处链接回填、图片独立上传、整体去重、创建保护及旧版本链接保留。已验证真实 B站元数据获取与单分 P 隔离；ASR、截图、抖音和实际飞书写入（含双归档）仍需在已配置环境联调。演示 note.json 不是对源视频的实际解读。

## 设计文档

参见 [视频笔记到飞书：Skill 设计方案](./视频笔记到飞书-Skill设计方案.md)，包含架构、飞书字段、状态流转、去重恢复、上游复用方案与验收标准。

## 参考项目

- [bilibili-douyin-notes](https://github.com/lucky-Luor/bilibili-douyin-notes)：计划作为字幕获取、本地转写、截图和课堂笔记模板的能力基础。
- [VidLens](https://github.com/junnhwan/vid-lens)：参考图文对应、时间出处、内容组织和交付校验流程。

已检查 bilibili-douyin-notes 的相关脚本与许可，默认固定提交版本并通过独立 checkout 调用，不打包上游源码。VidLens 作为流程参考。详情与第三方许可边界见 [上游说明](references/upstream.md)。

## 使用边界

Skill 由 Agent 执行，需要相应的飞书访问工具和视频处理环境。在表格新增链接不会自动启动处理。AI 服务是否收费取决于所使用的 Agent 或模型。
