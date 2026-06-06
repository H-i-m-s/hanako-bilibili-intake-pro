import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { BiliIntakeError } from "./errors.js";
import { getSettings } from "./settings.js";
import { prepareRuntime, runCollector } from "./runtime.js";
import { ensureDir, hashText, normalizeBilibiliSource, sanitizePathSegment, truncateText } from "./utils.js";

const CACHE_FILE = "transcript_cache.json";

function loadCache(runtime) {
  const p = path.join(runtime.dataDir, CACHE_FILE);
  return fs.readFile(p, "utf-8").then(JSON.parse).catch(() => ({}));
}

function saveCache(runtime, cache) {
  const p = path.join(runtime.dataDir, CACHE_FILE);
  return fs.writeFile(p, JSON.stringify(cache, null, 2), "utf-8").catch(() => {});
}

export async function ingestBilibiliVideo(input, ctx) {
  const source = normalizeBilibiliSource(input.source, input.page);
  if (!source) {
    throw new BiliIntakeError("source 不能为空，必须是 BV 号或 B站链接。", { code: "MISSING_SOURCE" });
  }

  const settings = getSettings(ctx);
  const runtime = await prepareRuntime(ctx, settings);
  const slotName = buildSlotName(source, input.page);
  const cacheKey = slotName;

  const shouldSaveToWorkspace = input.saveToWorkspace === true || settings.saveToWorkspace === true;

  // ── 有 summaryText 时直接从缓存读取（兼容旧调用方式）──
  if (input.summaryText) {
    const cache = await loadCache(runtime);
    const cached = cache[cacheKey];
    if (cached) {
      const result = {
        ok: true, source, outputDir: "",
        title: cached.title, description: cached.description || "", uploader: cached.uploader,
        duration: cached.duration, transcriptSource: cached.transcriptSource,
        transcriptText: cached.transcriptTextFull ? truncateText(cached.transcriptTextFull, settings.maxReturnedTranscriptChars) : "",
        transcriptTextFull: cached.transcriptTextFull || "",
        transcriptTextTimestamped: cached.transcriptTextTimestamped || cached.transcriptTextFull || "",
        transcriptTextPath: "", metadataPath: "", rawInfoPath: "", audioStreamsPath: "",
        audioPath: "", subtitleFiles: [], resultPath: "", fallbackLog: [],
      };
      await writeWorkspaceDoc(result, source, input.summaryText, settings, runtime);
      result.agentTextPreview = truncateText(result.transcriptText || "", settings.maxReturnedTranscriptChars);
      result.saveToWorkspaceActive = false; // summaryText 直传模式，文档已由插件直接生成
      result.summaryTempPath = "";
      return result;
    }
  }

  // ── 采集 ──────────────────────────────────────────
  const tmpRoot = path.join(os.tmpdir(), "bili-intake");
  const outputDir = await ensureDir(path.join(tmpRoot, slotName));

  const payload = {
    source,
    page: input.page,
    outputDir,
    whisperModel: input.whisperModel || settings.whisperModel,
    whisperDevice:
      typeof input.whisperDevice === "string" && input.whisperDevice.trim()
        ? input.whisperDevice.trim()
        : settings.whisperDevice,
    whisperLanguage: input.whisperLanguage ?? settings.whisperLanguage,
    forceTranscribe: input.forceTranscribe === true,
    returnTextLimit: typeof input.returnTextLimit === "number" && Number.isFinite(input.returnTextLimit)
      ? input.returnTextLimit
      : settings.maxReturnedTranscriptChars,
    sttProvider: input.sttProvider || settings.sttProvider,
  };

  const result = await runCollector(runtime, payload);

  // 标准化结果
  result.ok = true;
  result.requested = {
    source: input.source,
    normalizedSource: source,
    page: input.page || null,
    forceTranscribe: payload.forceTranscribe,
    whisperModel: payload.whisperModel,
    whisperDevice: payload.whisperDevice,
    whisperLanguage: payload.whisperLanguage || null,
    sttProvider: payload.sttProvider,
  };
  result.runtime = {
    mode: runtime.selectedMode,
    fallbackUsed: runtime.fallbackUsed === true,
    candidateModes: runtime.candidateModes,
    venvDir: runtime.venvDir,
  };

  // 写入缓存
  const cache = await loadCache(runtime);
  cache[cacheKey] = {
    title: result.title || "",
    description: result.description || "",
    uploader: result.uploader || "",
    duration: result.duration || 0,
    transcriptSource: result.transcriptSource || "",
    transcriptTextFull: result.transcriptTextFull || result.transcriptText || "",
    transcriptTextTimestamped: result.transcriptTextTimestamped || "",
  };
  await saveCache(runtime, cache);

  result.agentTextPreview = truncateText(result.transcriptText || "", payload.returnTextLimit);

  // ── 后台轮询：等待 Agent 写入临时总结文件（持续 5 分钟）──
  if (shouldSaveToWorkspace && !input.summaryText) {
    const bvMatch = source.match(/BV[0-9A-Za-z]+/);
    const bvId = (bvMatch && bvMatch[0]) || "";
    if (bvId && settings.workspacePath) {
      const tempPath = path.join(settings.workspacePath, `.summary_${bvId}.md`);

      // 在结果中暴露临时文件写入路径，Agent 通过此字段写入总结
      result.saveToWorkspaceActive = true;
      result.summaryTempPath = tempPath;

      // 立即检查一次：可能 Agent 已经写好了
      try {
        const preStat = await fs.stat(tempPath).catch(() => null);
        if (preStat) {
          const summary = (await fs.readFile(tempPath, "utf-8")).trim();
          await fs.unlink(tempPath).catch(() => {});
          if (summary) {
            const docResult = { ...result };
            await writeWorkspaceDoc(docResult, source, summary, settings, runtime);
          }
          return result;
        }
      } catch {}

      // 没写好就启动轮询
      const pollInterval = 1000;
      const pollTimeout = 300000; // 5 分钟
      const startTime = Date.now();

      const timer = setInterval(async () => {
        try {
          if (Date.now() - startTime >= pollTimeout) {
            clearInterval(timer);
            return;
          }
          const stat = await fs.stat(tempPath).catch(() => null);
          if (!stat) return;

          clearInterval(timer);
          const summary = (await fs.readFile(tempPath, "utf-8")).trim();
          await fs.unlink(tempPath).catch(() => {});

          if (summary) {
            const docResult = { ...result };
            await writeWorkspaceDoc(docResult, source, summary, settings, runtime);
          }
        } catch {}
      }, pollInterval);
    } else if (shouldSaveToWorkspace && !input.summaryText) {
      // workspacePath 未配置，标记为不活跃
      result.saveToWorkspaceActive = false;
      result.summaryTempPath = "";
    }
  } else {
    result.saveToWorkspaceActive = false;
    result.summaryTempPath = "";
  }

  return result;
}

async function writeWorkspaceDoc(result, source, summaryText, settings, runtime) {
  const wsDir = settings.workspacePath || runtime.dataDir;
  await ensureDir(wsDir);

  const fullText = result.transcriptTextTimestamped || result.transcriptTextFull || result.transcriptText || "";
  const bvMatch = source.match(/BV[0-9A-Za-z]+/);
  const bvId = (bvMatch && bvMatch[0]) || "unknown";
  const now = new Date().toISOString().replace(/[:.]/g, "-").slice(0, 19);
  const safeTitle = sanitizePathSegment(result.title || "video", 60);
  const filename = `${safeTitle}_${bvId}_${now}.md`;
  const filepath = path.join(wsDir, filename);

  let content;
  if (summaryText) {
    content = [
      `# B站视频总结 - ${result.title || ""}`,
      "",
      `- **标题**：${result.title || ""}`,
      `- **链接**：${source}`,
      `- **作者**：${result.uploader || ""}`,
      result.duration ? `- **时长**：${String(Math.floor(result.duration / 60))}分${String(Math.floor(result.duration % 60))}秒` : "",
      "",
      "---",
      "",
      "## AI 视频总结",
      "",
      summaryText,
      "",
      "---",
      "",
      "## 视频完整字幕",
      "",
      "```text",
      fullText,
      "```",
      "",
      "---",
      "",
      "*文档结束*",
    ].join("\n");
  } else {
    content = [
      `# B站视频转录 - ${result.title || ""}`,
      "",
      `- **标题**：${result.title || ""}`,
      `- **链接**：${source}`,
      `- **作者**：${result.uploader || ""}`,
      result.duration ? `- **时长**：${String(Math.floor(result.duration / 60))}分${String(Math.floor(result.duration % 60))}秒` : "",
      `- **转录来源**：${result.transcriptSource || ""}`,
      `- **时间**：${now}`,
      "",
      "---",
      "",
      "## 完整原文",
      "",
      "```text",
      fullText,
      "```",
      "",
      "---",
      "",
      "*文档结束*",
    ].join("\n");
  }

  await fs.writeFile(filepath, content, "utf-8");
}

function buildSlotName(source, page) {
  const url = new URL(source);
  const pageSuffix = page && Number(page) > 1 ? `-p${page}` : "";
  const token = sanitizePathSegment(url.pathname.split("/").filter(Boolean).pop() || "video");
  return `${token}${pageSuffix}-${hashText(source)}`;
}

export function formatAgentPayload(result) {
  const parts = [
    "B站视频采集完成。",
    `标题: ${result.title || ""}`,
  ];

  if (result.uploader) {
    parts.push(`UP主: ${result.uploader}`);
  }
  if (result.description) {
    parts.push(`简介: ${result.description}`);
  }
  if (result.duration) {
    parts.push(`时长(秒): ${result.duration}`);
  }

  if (result.runtime?.mode) {
    parts.push(`运行模式: ${result.runtime.mode}`);
    if (result.runtime.fallbackUsed) {
      parts.push("运行模式说明: 已从默认候选回退到备用环境。");
    }
  }
  parts.push(`字幕来源: ${result.transcriptSource}`);
  if (result.transcriptDevice) {
    parts.push(`转写设备: ${result.transcriptDevice}`);
  }

  if (Array.isArray(result.fallbackLog) && result.fallbackLog.length > 0) {
    parts.push("转录降级链路:");
    for (const entry of result.fallbackLog) {
      const statusIcon = entry.status === "success" ? "✅" : entry.status === "skipped" ? "⏭️" : "❌";
      parts.push(`  ${statusIcon} ${entry.layer}: ${entry.status}${entry.detail ? ` (${entry.detail})` : ""}${entry.reason ? ` — ${entry.reason}` : ""}`);
    }
  }

  // 暴露工作总结所需字段
  parts.push(`saveToWorkspaceActive: ${result.saveToWorkspaceActive === true ? "true" : "false"}`);
  if (result.summaryTempPath) {
    parts.push(`summaryTempPath: ${result.summaryTempPath}`);
  }

  parts.push("", "以下为视频字幕内容（完整文本见返回结果的 transcriptTextFull 字段，带时间戳版本见 transcriptTextTimestamped 字段）：", result.agentTextPreview || "");
  return parts.join("\n");
}
