# Bilibili Intake Pro

**MANDATORY TRIGGERS:** bilibili, B站, BV号, av号, 视频字幕, 视频转录, 视频总结, 视频内容提取

采集B站视频字幕、生成带时间戳的AI总结、保存到工作区。当用户给出 BV 号 / av 号 / B站视频链接，并且明确希望获取字幕、总结或保存视频内容时，优先使用此工具。

优先调用工具：`bilibili_video_intake`

## 推荐调用场景

- "帮我读这个 BV 视频内容"
- "把这个 B 站视频转成可读文本"
- "先抓字幕，没有字幕就转写"
- "把视频标题、简介、音频、字幕都落本地"

## 建议参数

```json
{
  "source": "BVxxxxxxxxxx",
  "page": 1,
  "forceTranscribe": false,
  "saveToWorkspace": false,
  "sttProvider": "mimo"
}
```

## 命令式工作流（必须严格执行）

### 第 1 步：调用工具获取字幕

```json
{ "source": "BVxxxxxxxxxx" }
```

从返回结果的 `transcriptTextFull` 获取完整字幕。

### 第 2 步：检查是否需要写临时文件

在工具返回的文本中查找 `saveToWorkspaceActive:` 字段，形如 `saveToWorkspaceActive: true` 或 `saveToWorkspaceActive: false`。

- **`true`** → 继续查找 `summaryTempPath:` 字段获得写入路径（形如 `summaryTempPath: D:\Agent\B站视频总结\.summary_BV1xx.md`），**记住这个路径，下一步必须用 write 工具写入**
- **`false`** → 不需要写文件，直接跳到第 3 步

> 如果 `saveToWorkspaceActive: true` 但 `summaryTempPath` 为空或不存在，说明工作区路径未配置，跳过写文件。

### 第 3 步：生成总结并展示

**在此处直接生成总结内容，输出到对话中展示给用户看。**

如果第 2 步标记了 `saveToWorkspaceActive: true`，在生成总结的**同时**用 write 工具把总结写入 `summaryTempPath`。

总结用 Markdown 书写，格式如下（四个区块，缺一不可）：

 `[视频标题]`

 视频简介：用一句话概括视频的核心内容和主题。

 **【内容概要】**
 按视频的叙事顺序，分 3~6 个要点总结核心内容。每个要点标注时间戳 `[MM:SS]`，要点之间空一行。每个要点 1~3 句话。

 **【关键观点】**
 列出视频中的重要观点、技术细节、结论。每个观点标注时间戳 `[MM:SS]`，观点之间空一行。每个观点 1~2 句话。

 **【个人思考】**
 延伸思考：指出可能的问题、补充信息、相关话题。不需要时间戳。1~3 段，每段 1~3 句话。

## 返回字段说明

1. 读取返回结果里的 `transcriptText`
2. 通过 `fallbackLog` 了解转写来源链路
3. 如果需要完整内容，使用 `transcriptTextFull` 字段（`transcriptTextPath` 指向的临时文件已被自动清理，不要读取）
4. 如需核对来源或重新处理，查看：
   - `metadataPath`
   - `audioStreamsPath`
   - `audioPath`
   - `subtitleFiles`
5. 根据 `saveToWorkspace` 判断是否需要读取 workspace 下的原始转录文件

## fallbackLog 示例

```json
"fallbackLog": [
  {"layer": "CC字幕", "status": "skipped", "reason": "无可用CC字幕"},
  {"layer": "AI字幕(ai-zh)", "status": "success", "detail": "下载成功，12847字"},
  {"layer": "云端 STT (mimo)", "status": "skipped", "reason": "前一层已成功"},
  {"layer": "Whisper", "status": "skipped", "reason": "前一层已成功"}
]
```
