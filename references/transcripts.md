# 转写质检、校正和语言

在需要本地 ASR、核对转写、修正术语或逐段阅读长视频时读取。本页的命令只写本地文件，不发布飞书，也不调用额外模型 API。

## 获取与留存

字幕仍优先；完整字幕与 ASR 全文都会经过结构质检，在同一任务版本目录留存：

- `transcripts/raw.txt`：保留来源语言的原稿，不覆盖成译文或总结。
- `transcripts/raw.segments.json`：按原文顺序记录 `id`、`start`（全局秒数）、`text`。上游只有开始时间，不编造结束时间或说话人。
- `transcripts/quality.json`：首次留存的原稿质检。
- `transcripts/provenance.json`：来源身份、时长、来源类型、原稿和校正版的路径及 SHA-256。
- `transcripts/corrected.txt`、`corrected.segments.json`、`correction-quality.json`：实际完成校正后才生成。

`manifest.transcript` 指向当前可用全文：初次为原稿，校正通过后为校正版。`raw_transcript` 始终指向原稿。笔记和截图关键词使用当前全文；引用或不确定内容仍须回查原稿、相邻时间段，必要时听音频。

长视频分片和短视频整段 ASR 均使用固定上游的模型与术语接口。每片检查非空文本的时间格式、顺序和范围，完整合并后再检查全文。无任何有效文本、无法定位的非空行、空文本段落、时间倒退或越界会阻止标记完成；具体错误保留在本地质检 JSON。空白 ASR 窗口作为“可能静音”检查点保存，允许继续后面的窗口；整片音频都无有效文字时质检失败。

连续重复文字、可能静音的窗口、末条文字开始时间距离片尾超过两分钟会提示人工复核，不自动删除或补写内容。结构通过不代表听写准确、没有漏听或能够识别发言人；片尾提示也不等于确有缺失。需抽查开头、中段、结尾及支撑核心结论的片段。

## 热词和语言

`config.json` 可配置 `asr_language`（默认 `auto`）和 `asr_hotwords`（字符串数组）。它们与笔记展示语言 `language` 独立。单个任务可覆盖语言、追加术语：

```powershell
python scripts/vfn.py video prepare "<视频链接>" --record-id <record_id> --version <版本> --transcribe --asr-language en --hotword NVIDIA --hotword CUDA
```

中文明确时选 `zh`，英文选 `en`，其他语言用 Whisper 语言代码；中英混合或未知语言选 `auto`。`auto` 按窗口自动检测主要语言，不保证一个窗口内每种语言都识别完整，不翻译原稿。

术语由显式词表、视频标题、作者、分 P 标题生成，去重并限制为 20 条、总计不超过 400 字符，通过固定上游的 `terms`/`initial_prompt` 接口传入 ASR。提示词不是识别正确性的证据；不要依据识别结果不断修改热词重跑。

首次 ASR 保存 `asr-settings.json`，语言、词表、请求的模型与设备同时绑定检查点。续跑重复使用相同配置和命令参数；修改语言或词表需要新版本。模型实际降级仍以返回的 `model_used`、`device_used`、`fallback_reasons` 为准。旧版尚未完成的检查点没有语言、词表身份，不能与新设置混合续跑；保留旧目录，另建版本。旧版完整素材可以继续读取，并在再次 prepare 时补留原稿。

## 可选校正

只校正确认过的人名、品牌、术语或明显识别错误；不把摘要、中文译文或补充解释放进逐字稿。无需修正时直接用原稿写笔记，不为完成流程生成无变化的校正版。

复制 `raw.segments.json` 的数组，保持条数、`id` 和 `start` 不变，只编辑 `text`，放入以下对象，保存为同目录的 `corrections.json`：

```json
{
  "source_key": "与 manifest.source.key 完全一致",
  "segments": [
    {"id": 1, "start": 0, "text": "本段经核对后的原语言文字"}
  ]
}
```

上例只展示结构，实际必须包含原稿的全部段落。然后执行：

```powershell
python scripts/vfn.py video finalize-transcript --manifest output/<record_id>/v<版本>/manifest.json --corrections output/<record_id>/v<版本>/corrections.json
```

段数、编号或时间改变、空段和非法时间会被拒绝。校正总字符数低于原稿 70% 时警告，低于 50% 时拒绝。字符保留率仅用于发现大段删减，不能证明校正忠实；不得为过检查填充文字。失败报告为 `last-correction-check.json`，原稿和已选全文保持不变。

成功后保存独立校正版并原子更新 manifest，原稿不改写；同一校正可以重试，新的不同校正使用新版本。发布前重新校验本地原稿与校正版的哈希、来源和时间轴；发现留存文件被手改时停止，恢复原文件或开启新版本。再次 prepare 不会把校正版覆盖回原稿。

原稿和校正版是本地核验素材，默认不作为额外全文文档上传飞书。飞书仍保存生成笔记；是否发布或生成新版本遵循主 Skill 的任务授权。

## 长视频阅读索引

完整素材在 prepare 或 finalize-transcript 成功时自动生成本地阅读包：

- `manifest.reading_index_markdown`：时间窗口表与阅读文件链接。
- `manifest.reading_index`：机器可读 JSON，含来源、全文哈希、窗口、片段、原段落编号及无段落起点窗口。
- 同目录 `part-0001.md` 等：来源标题、链接、全文哈希、原段落编号与真实起点，以及该片的完整原文。
- `manifest.reading_progress`：可变阅读进度。manifest 中的 reading_coverage 是最近命令执行时的快照。

文件位于 `transcripts/reading/<内容与版本指纹>/`。按原段落起点分到十分钟窗口，窗口内超过 12,000 个原文字字符时按完整段落拆片；特别长的单段会整体保留，可超过这个软上限，不截断原文。全部片段按时间顺序覆盖当前全文，每个原段落出现一次。窗口边界是导航范围，不是推测的说话结束时间；无段落起点可能只是前段很长，不能据此断言静音或漏转写。

阅读顺序：读索引、逐片读原文、结合相邻片段分析，实际读完后才记录状态：

```powershell
python scripts/vfn.py video reading-index --manifest output/<record_id>/v<版本>/manifest.json
python scripts/vfn.py video reading-index --manifest output/<record_id>/v<版本>/manifest.json --mark-read part-0001 part-0002
```

第一个命令校验留存原稿与校正版后重建派生索引；第二个只标记提供的有效片段 ID，重复标记幂等。检查 `reading_coverage.unread_parts`，不要把索引、预览或“已读”标记当作阅读全文的替代。已读状态是本地工作记录，不是识别准确或内容完整性的证明。

同稿重建保留进度，切换校正版产生新索引、从未读开始，旧阅读包继续留存。未完成转写、质检失败或留存稿被修改时拒绝生成。旧素材没有 provenance 时先再次 prepare 补留原稿。索引只服务本地阅读，不增加飞书全文归档。
