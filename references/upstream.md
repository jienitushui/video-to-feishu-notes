# 上游来源与实现取舍

## bilibili-douyin-notes

- 仓库：https://github.com/lucky-Luor/bilibili-douyin-notes
- 固定版本：2497a7f564670c840f5e1c4055c8a536237cd5f3
- 已检查：SKILL.md、LICENSE、B站/抖音获取、本地转写与截图脚本的命令、元数据结构。
- 复用方式：独立本地 checkout，由子进程调用；本仓库不打包上游脚本。
- 真实验证：2026-10-20 用上游示例的 B站第 19 分 P 验证元数据与页面隔离；未配置登录态，未取得可用字幕。ASR、截图和抖音抓取尚未完成真实联调。

上游 B站 fetch 默认读取所有分 P。本仓库 bili_one.py 运行其未改写函数，在视频信息响应中只保留选定页面，避免遍历合集；文件名规则匹配固定版本。短链先解析实际 URL，保留分 P。

`view` 元数据接口失败（包括 HTTP 412）时，封装层回退到视频网页的 `__INITIAL_STATE__.videoData` 获取标题、UP主、aid 等信息，并通过 `x/player/pagelist` 获取分 P、cid 与时长，再按原规则筛选指定页面。网页信息必须与请求的 BV 号一致且字段完整；网页或 pagelist 也失败时报告两条路径的错误，不用占位元数据继续处理。该适配不修改上游 checkout、固定版本、字幕接口或 playurl 请求。

prepare 输出 selected-metadata.json 和 manifest.json。转写和截图只处理选中页面，工作目录绑定原始输入，不能跨视频混用。

上游主体 MIT。抖音 abogus.py 由上游首次使用时取得，其 README 对该组件说明 GPLv3 许可例外；请遵循实际组件条款。本仓库不分发该组件。升级前复查接口、依赖、许可，并固定完整提交 SHA。

## VidLens

- 仓库：https://github.com/junnhwan/vid-lens
- 参考 README 的全片理解、真实画面支撑章节、时间出处、进度可回看和交付核对。
- 未复制代码、不运行其后端、不调用公共网站作为任务 API。

理解与写作由宿主 Agent 完成，无需同时部署两个项目。

## 飞书接口依据

适配器依据开放平台与官方 larksuite/oapi-sdk-python 模型检查请求结构。部署前需在实际租户上进行小规模联调。

- 官方 SDK：https://github.com/larksuite/oapi-sdk-python
- 表格记录：https://open.feishu.cn/document/server-docs/docs/bitable-v1/app-table-record/list
- 创建文档：https://open.feishu.cn/document/server-docs/docs/docs/docx-v1/document/create
- 创建子块：https://open.feishu.cn/document/server-docs/docs/docs/docx-v1/document-block-children/create
- 素材上传：https://open.feishu.cn/document/server-docs/docs/drive-v1/media/upload_all

使用 API token 身份访问资源，无浏览器模拟。真实飞书写入待配置后联调，离线测试不等于线上 API 兼容性保证。
