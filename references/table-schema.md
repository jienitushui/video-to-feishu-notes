# 多维表格字段与适配约定

CLI 在现有表内创建下列字段，不创建新的 Base，不自动修改视图。config.json 的 fields 可映射字段名，所有 Agent 应使用相同映射。

| 逻辑字段 | 默认名称 | 飞书类型 | 说明 |
|---|---|---|---|
| task_name | 任务名称 | 文本 1 | 主字段；新建任务先写入分享文本，识别后更新为视频标题 |
| url | 视频链接 | 文本 1 | 原始分享文本或链接 |
| enabled | 开始生成 | 复选框 7 | 默认不处理未勾选任务 |
| status | 处理状态 | 单选 3 | 待处理、处理中、已完成、失败、待人工处理 |
| stage | 当前阶段 | 文本 1 | 当前操作 |
| title / platform | 视频标题 / 视频平台 | 文本 1 | 元数据 |
| category / requirements | 学习分类 / 笔记要求 | 文本 1 | 首版分类精确匹配文本 |
| document | 笔记文档 | 超链接 15 | 最近成功版本的主文档；both 模式为知识库页面 |
| folder_document | 备份文档 | 超链接 15 | both 模式生成时的云盘备份链接；单目标模式为空 |
| summary | 内容摘要 | 文本 1 | 最近成功版本摘要 |
| regenerate | 重新生成 | 复选框 7 | 创建新版本，全部成功并确认表格链接后删除旧主文档与备份 |
| completed_at | 完成时间 | 日期 5 | 毫秒 Unix 时间 |
| error / result | 错误信息 / 结果来源 | 文本 1 | 原因、新生成或复用 |
| source_key / fingerprint | 视频唯一标识 / 输入指纹 | 文本 1 | 视频及输入去重 |
| run_id | 执行标识 | 文本 1 | 当前执行所有权 |
| updated_at | 最近更新时间 | 日期 5 | 最近检查点 |
| attempts / version | 尝试次数 / 生成版本 | 数字 2 | 次数与版本 |
| document_id | 工作文档ID | 文本 1 | 当前草稿 |
| state | 任务检查点 | 文本 1 | 紧凑 JSON |

映射示例：

    "fields": {"url": "原视频", "document": "课堂笔记", "state": "系统检查点"}

检查点保存 input、generation、run_id、source、fingerprint、publish、published。input 包含原始链接、笔记要求、模板版本和输出语言。publish 保存内容哈希、创建尝试、标题、已写数量、destination（归档目标）；wiki 模式另存 wiki_node（node_token 与 obj_token）。published 保存已成功文档 ID、URL、哈希、destination 及可选 wiki_node_token；复用时记录来源。both 模式由 publish.targets.folder/wiki 分别保存备份与主文档的创建、正文 ID、块进度和已校验结果，published.targets 保留两处最终结果；工作文档ID存云盘备份正文 ID，知识库主文档 ID存检查点。知识库链接使用节点 token；“笔记文档”指向知识库，“备份文档”指向云盘生成副本。两份成功后统一回填这两列，任一处未完成不清除上一成功版本链接。单目标成功清空“备份文档”。不要放完整字幕、图片字节或凭据。

旧表可将“云盘笔记”重命名为“备份文档”，或映射 folder_document 保留旧名称。wiki_document 是兼容旧配置的可选逻辑字段，仅在 fields 显式映射时创建、校验和回填，默认不再生成“知识库笔记”列。schema --apply 只补缺失字段，不删除旧列或迁移历史链接；详见 setup.md。

默认筛选 enabled=true、有链接、待处理；已完成且重新生成=true 也可入队。明确重试时才纳入失败记录。空状态视为待处理；分类为精确文本匹配。

version_cleanup 保存待清理旧文档的 document_id、url、destination、可选 wiki_node_token、status（pending/deleting/deleted）及 error。只有新版发布和图片校验、表格链接更新并回读确认全部成功后才执行删除；清理失败不改变新版已完成状态，可单独重试。旧版元数据与表格链接不一致时不入队，已有历史版本不追溯清理。详见 state-and-recovery.md。

MCP 适配需要：分页读记录、按 ID 查询/更新、字段定义读写（初始化）、创建文档、读文档及子块、追加块、上传和替换图片，以及删除旧文档、区分资源已删除与权限错误的能力。真实工具名与参数从环境说明获取。

飞书是状态权威来源，本地完成文件不能替代飞书判断。首版单执行者，普通表格更新不提供多 Agent 互斥。
