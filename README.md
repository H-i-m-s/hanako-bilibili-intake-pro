# Bilibili Intake Pro v1.0.0

一个面向 **OpenHanako Agent** 的 B 站视频采集插件，支持四层字幕降级、AI 自动总结、带时间戳的 `.md` 输出。

---

## 功能特性

- **四层字幕降级**：CC 字幕 → AI 字幕 → MiMo ASR（云端）→ Whisper（本地）
- **Cookie 免配置**：通过插件设置界面粘贴 SESSDATA + bili_jct，自动生成 cookies.txt
- **智能模型选择**：auto 模式下根据 GPU 显存和视频时长自动选 Whisper 模型
- **带时间戳的字幕**：生成的 `.md` 文件中每行字幕前标注 `[HH:MM:SS]`
- **AI 视频总结**：Agent 生成总结后写入临时文件，插件后台自动合并
- **零残留**：采集过程中产生的临时文件在完成后自动清理

---

## 用户配置指南

### 1. 基本配置

在 HanaAgent 的插件设置界面中找到 **Bilibili Intake Pro**，按以下步骤配置：

| 配置项 | 推荐值 | 说明 |
|--------|--------|------|
| `saveToWorkspace` | `true` | 开启后将生成带总结的 `.md` 文件到工作区 |
| `workspacePath` | `D:\Agent\B站视频总结` | 工作区目录，生成的 `.md` 文件存放在这里 |
| `whisperModel` | `auto` | 自动根据显卡和视频时长选择最佳模型 |
| `maxReturnedTranscriptChars` | `16000` | 返回给 Agent 的字幕字符上限 |

### 2. B站 Cookie 配置（必需，否则无法下载 AI 字幕）

1. 打开 Chrome/Edge，登录 bilibili.com
2. 按 F12 打开开发者工具 → Application（或 应用）→ Cookies → `www.bilibili.com`
3. 找到 `SESSDATA`，复制 Value 值 → 粘贴到插件设置的 `sessdata` 字段
4. 找到 `bili_jct`，复制 Value 值 → 粘贴到插件设置的 `biliJct` 字段
5. 保存设置，插件会自动生成 `cookies.txt`

> SESSDATA 有效期约 30 天，过期后重复上述步骤更新即可。

### 3. MiMo ASR 配置（可选，第3层降级用）

如需云端语音转写作为字幕兜底，需配置 API key：

1. 打开 `python/providers.json`
2. 将 `api_key` 填入你的 MiMo API key：

```json
{
  "stt": {
    "mimo": {
      "enabled": true,
      "api_base": "https://token-plan-cn.xiaomimimo.com/v1",
      "api_key": "你的API_KEY",
      "model": "mimo-v2.5-asr",
      "language": "auto"
    }
  }
}
```

### 4. Whisper 环境配置（可选，第4层降级用）

插件默认使用 conda 的 `Agent` 环境中的 Python，已预装 torch 2.11.0+cu128 和 openai-whisper。

如需自定义 Python 路径，在插件设置中填写 `pythonPath`（如 `E:\Conda\envs_dirs\Agent\python.exe`）。

### 5. 其他

插件支持`保存到工作空间`与`不保存`热切换，切换完记得点保存

---

## Agent 使用流程

1. Agent 调用 `bilibili_video_intake` 工具传入 `source`
2. 插件采集字幕返回给 Agent，同时在后台轮询等待临时总结文件（5 分钟超时）
3. Agent 生成 AI 总结，用 `write` 工具写入 `.summary_{bvId}.md` 到工作区
4. 插件检测到临时文件，自动合并字幕和总结，生成最终 `.md`，删除临时文件

---

## 项目结构

```
bilibili-intake-pro/
├── manifest.json                  # 插件配置
├── package.json                   # npm 包信息
├── README.md                      # 本文件
│
├── tools/
│   └── bilibili_video_intake.js   # OpenHanako 工具入口
│
├── lib/
│   ├── service.js                 # 工具层编排与 Agent 返回格式
│   ├── runtime.js                 # Python 运行时准备
│   ├── settings.js                # 配置读取
│   ├── constants.js               # 常量与默认值
│   ├── errors.js                  # 错误类型
│   ├── utils.js                   # 工具函数
│   └── tool-output.js             # 工具返回格式化
│
├── python/
│   ├── collector.py               # 四层降级主链路
│   ├── stt_providers.py           # STT provider 调用模块
│   ├── providers.json             # Provider 配置（API key）
│   └── requirements.txt           # Python 依赖
│
├── skills/
│   └── bilibili-video-intake/
│       └── SKILL.md               # Agent skill 说明
│
├── scripts/
│   └── package_release.py         # 打包脚本
│
└── tests/
    └── smoke.mjs                  # 轻量 smoke test
```

---

## 工具接口

工具名：`bilibili_video_intake`

### 输入参数

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `source` | string | 是 | BV号、av号或 B站视频链接 |
| `page` | number | 否 | 分P页码 |
| `forceTranscribe` | boolean | 否 | 跳过字幕和云端 ASR，直接 Whisper |
| `whisperModel` | string | 否 | auto（智能选择）或 base/small/medium/large |
| `whisperDevice` | string | 否 | auto / cuda / cpu |
| `whisperLanguage` | string | 否 | 语言（如 zh / en），留空自动识别 |
| `returnTextLimit` | number | 否 | 回传正文字符上限 |
| `saveToWorkspace` | boolean | 否 | 是否保存到工作区（配合后台轮询使用） |
| `sttProvider` | string | 否 | STT provider 名称，如 mimo |
| `summaryText` | string | 否 | 直接传入 AI 总结文本（不走轮询路径） |

### 返回字段

| 字段 | 说明 |
|------|------|
| `title` | 视频标题 |
| `description` | 视频简介 |
| `uploader` | UP主 |
| `duration` | 时长（秒） |
| `transcriptSource` | 文本来源 |
| `transcriptText` | 给 Agent 的正文预览 |
| `transcriptTextFull` | 完整字幕文本 |
| `transcriptTextTimestamped` | 带 `[HH:MM:SS]` 时间戳的字幕文本 |
| `fallbackLog` | 降级链路日志 |

---

## 字幕降级策略

```
第1层：CC 字幕（人工上传，100% 准确，秒出）
  ↓ 没有或为空
第2层：AI 字幕（B站自动生成，需 cookie）
  ↓ 没有或为空  
第3层：MiMo ASR（云端 API，自动切片并发）
  ↓ 失败或无 API key
第4层：Whisper 本地转写（GPU 优先，智能选模型）
```

---

## 参考
https://github.com/hyjump/hanako-bilibili-intake

https://github.com/54Lynnn/bilibili-auto-transcript