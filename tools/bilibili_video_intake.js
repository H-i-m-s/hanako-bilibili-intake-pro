import { BiliIntakeError } from "../lib/errors.js";
import { ingestBilibiliVideo, formatAgentPayload } from "../lib/service.js";
import { toToolError, toToolResult } from "../lib/tool-output.js";

export const name = "bilibili_video_intake";
export const description = "采集B站视频字幕（四层降级：CC→AI→MiMo ASR→Whisper）。一次调用，采集后返回字幕给 Agent 并后台轮询等待临时总结文件（5分钟超时）。Agent 将 AI 总结写入工作区 .summary_{bvId}.md 后，插件自动合并成带总结的 .md 文件。返回结果中包含 saveToWorkspaceActive（是否需写临时总结）和 summaryTempPath（Agent 应写入的临时总结文件路径）。";
export const parameters = {
  type: "object",
  properties: {
    source: {
      type: "string",
      description: "BV号、av号或 B站视频链接。",
    },
    page: {
      type: "number",
      description: "可选分P页码；source 已经包含 p 参数时通常不需要。",
    },
    forceTranscribe: {
      type: "boolean",
      description: "即使平台字幕存在，也强制使用 Whisper 转写。",
    },
    whisperModel: {
      type: "string",
      description: "可选覆盖配置中的 Whisper 模型名；auto=智能选择（根据 GPU/显存/时长自动选择），或指定模型名如 base/small/medium/large。默认 auto。",
    },
    whisperDevice: {
      type: "string",
      description: "可选覆盖配置中的转写设备：auto / cuda / cpu。默认 auto，会优先尝试 GPU。",
    },
    whisperLanguage: {
      type: "string",
      description: "可选覆盖配置中的 Whisper 语言，如 zh / en；留空自动识别。",
    },
    returnTextLimit: {
      type: "number",
      description: "可选覆盖回传给 Agent 的最大正文字符数。",
    },
    saveToWorkspace: {
      type: "boolean",
      description: "可选，是否保存到工作区。配合后台轮询使用：Agent 写入总结到 .summary_{bvId}.md 临时文件，插件自动合并生成最终 .md。默认为 false，可在插件配置中设置默认值。",
    },
    summaryText: {
      type: "string",
      description: "可选，直接传入 AI 总结文本（代理自行生成后传入，不走临时文件+轮询路径）。",
    },
    sttProvider: {
      type: "string",
      description: "可选，STT provider 名称（如 mimo），覆盖配置中的默认值。",
    },
  },
  required: ["source"],
};

export async function execute(input = {}, ctx) {
  try {
    if (!input.source || typeof input.source !== "string") {
      throw new BiliIntakeError("source 必须是 BV 号或链接字符串。", { code: "INVALID_SOURCE" });
    }
    const result = await ingestBilibiliVideo(input, ctx);
    return toToolResult(result, formatAgentPayload(result));
  } catch (error) {
    return toToolError(error, {
      action: name,
      source: input.source || null,
    });
  }
}
