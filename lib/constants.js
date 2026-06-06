import path from "node:path";

export const PLUGIN_ID = "bilibili-intake-pro";
export const PLUGIN_VERSION = "0.2.0";

export const DEFAULT_SETTINGS = Object.freeze({
  runtimeMode: "auto",
  pythonPath: "",
  nativePythonCommand: process.platform === "win32" ? "python" : "python3",
  wslPythonCommand: "python3",
  sttProvider: "mimo",
  whisperModel: "auto",
  whisperDevice: "auto",
  whisperLanguage: "",
  preferredSubtitleLanguages: ["zh-Hans", "zh-CN", "zh", "ai-zh", "zh-TW", "en", "ja"],
  audioFormat: "mp3",
  sessdata: "",
  biliJct: "",
  cookiesFile: "",
  cookieBrowser: "auto",
  saveToWorkspace: false,
  workspacePath: "",
  maxReturnedTranscriptChars: 12_000,
});

export const RUNTIME_DIR = ".runtime";
export const CAPTURES_DIR = "captures";
export const REQUIREMENTS_FILE = "python/requirements.txt";
export const COLLECTOR_SCRIPT = "python/collector.py";
export const PROVIDERS_CONFIG = "python/providers.json";
export const REQUIRED_PYTHON_PACKAGES = ["yt-dlp", "openai-whisper", "requests"];
