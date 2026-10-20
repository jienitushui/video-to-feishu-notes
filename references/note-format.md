# 笔记内容与 note.json

先阅读素材 manifest、完整转写稿和所选画面，再组织结构。长视频分段理解并合并检查覆盖与重复。字幕或截图不能支持的结论应标记未展开或转写不确定，不自行补成原话。

推荐内容：核心内容、知识小节、知识地图、概念卡片、步骤或代码、自测与答案、回看索引和未确认内容。短视频可合并，结构与实际内容相称。知识地图用层级文本或列表。

以上游课堂模板为内容基础，采用 VidLens 的图文对应原则：截图支撑具体解释，标注实际时间；清晰但无关的画面不作为证据。QC 后仍应视觉复核。时间来自字幕或抽帧清单。抖音显示时间并链接原片，不宣称精确跳转。

## 中间格式

note.json 由 Agent 编写，脚本仅验证与发布，不调用额外模型 API。examples/note.json 是演示格式，不是实际视频解读。

顶层字段：schema_version=1；非空 title（最多 200 字符）；summary（最多 2000）；source_key（与素材 manifest.source.key 相同）；sections（1–100 节）。每节包含唯一 id、title、非空 blocks。

| type | 字段 | 写入效果 |
|---|---|---|
| text | text，可选 seconds | 段落 |
| bullet | text，可选 seconds | 列表项 |
| code | text，可选 language、seconds | 飞书纯文本代码块，language 用于 Markdown 导出 |
| image | path、caption、seconds | 回看链接、图片、说明 |

text 内容是纯文本，首版不解释正文的 Markdown 加粗、HTML 或表格。对比可用列表表达。来源和回看链接由发布器根据可信元数据生成。

图片相对 note.json 所在目录，必须位于该工作目录内；支持 png/jpg/jpeg/webp，最大 20 MiB。禁止本机绝对路径、上级目录和远程图片 URL。换设备需保留相同相对路径和图片内容。

同代笔记首次发布后冻结，内容或图片变化会触发哈希不一致。恢复原文件继续，或显式开启新版本。

## 校验与 Markdown 导出

    python scripts/vfn.py validate-note <note.json>
    python scripts/vfn.py render-markdown <note.json> --manifest <manifest.json> --output <note.md>

validate-note 检查结构、图片和时间类型；发布时还检查来源、时间范围、远端块完整性。忠实性、概念质量和题目是否适合由 Agent 判断。

Markdown 建议导出到 note.json 同目录，以保持相对图片链接。飞书使用结构化块写入。上游 v3 Markdown 校验器针对不同语法，首版不直接调用它校验 note.json。
