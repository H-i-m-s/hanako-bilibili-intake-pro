import assert from "node:assert/strict";
import { buildCudaWheelTags, parseCudaVersionFromNvidiaSmi, parseHelperJson, resolveRuntimeModePlan } from "../lib/runtime.js";
import { normalizeBilibiliSource, sanitizePathSegment, truncateText } from "../lib/utils.js";
import { formatAgentPayload } from "../lib/service.js";
import * as tool from "../tools/bilibili_video_intake.js";

assert.equal(normalizeBilibiliSource("BV1xx411c7mD"), "https://www.bilibili.com/video/BV1xx411c7mD");
assert.equal(
  normalizeBilibiliSource("https://www.bilibili.com/video/BV1xx411c7mD", 3),
  "https://www.bilibili.com/video/BV1xx411c7mD?p=3",
);
assert.equal(sanitizePathSegment("标题 / test?"), "标题-test");
assert.equal(truncateText("abcdef", 4), "abc…");
assert.equal(tool.name, "bilibili_video_intake");
assert.deepEqual(resolveRuntimeModePlan("auto", "win32"), ["native", "wsl"]);
assert.deepEqual(resolveRuntimeModePlan("auto", "linux"), ["native"]);
assert.deepEqual(resolveRuntimeModePlan("native", "win32"), ["native"]);
assert.deepEqual(resolveRuntimeModePlan("wsl", "win32"), ["wsl"]);
assert.equal(
  parseCudaVersionFromNvidiaSmi("NVIDIA-SMI 591.74 Driver Version: 591.74 CUDA Version: 13.1"),
  "13.1",
);
assert.deepEqual(buildCudaWheelTags("13.1"), ["cu130", "cu128", "cu126", "cu124", "cu121", "cu118"]);
assert.deepEqual(buildCudaWheelTags("12.4"), ["cu124", "cu121", "cu118"]);
assert.deepEqual(buildCudaWheelTags("11.8"), ["cu118"]);
assert.deepEqual(
  parseHelperJson('Detected language: Chinese\n{"ok":true,"title":"示例"}'),
  { ok: true, title: "示例" },
);

const preview = formatAgentPayload({
  title: "示例视频",
  uploader: "示例UP",
  description: "这是简介",
  duration: 123,
  transcriptSource: "platform_subtitle",
  outputDir: "/tmp/demo",
  metadataPath: "/tmp/demo/metadata.json",
  rawInfoPath: "/tmp/demo/raw_info.json",
  transcriptTextPath: "/tmp/demo/transcript.txt",
  audioPath: "/tmp/demo/audio.mp3",
  subtitleFiles: ["/tmp/demo/subtitle.zh.vtt"],
  audioStreamsPath: "/tmp/demo/audio_streams.json",
  transcriptDevice: "cuda",
  runtime: {
    mode: "native",
    fallbackUsed: false,
  },
  agentTextPreview: "第一段文字",
});
assert.match(preview, /示例视频/);
assert.match(preview, /第一段文字/);
assert.match(preview, /运行模式: native/);
assert.match(preview, /转写设备: cuda/);

console.log("smoke ok");
