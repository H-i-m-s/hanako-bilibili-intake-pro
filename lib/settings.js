import path from "node:path";
import { DEFAULT_SETTINGS } from "./constants.js";

function readConfig(ctx, key) {
  try {
    return ctx?.config?.get?.(key);
  } catch {
    return undefined;
  }
}

export function getSettings(ctx) {
  const preferredSubtitleLanguages = parseLanguages(
    readConfig(ctx, "preferredSubtitleLanguages") ?? DEFAULT_SETTINGS.preferredSubtitleLanguages.join(","),
  );

  return {
    runtimeMode: normalizeMode(readConfig(ctx, "runtimeMode") ?? DEFAULT_SETTINGS.runtimeMode),
    pythonPath: stringify(readConfig(ctx, "pythonPath")) || DEFAULT_SETTINGS.pythonPath,
    nativePythonCommand:
      stringify(readConfig(ctx, "nativePythonCommand")) || DEFAULT_SETTINGS.nativePythonCommand,
    wslPythonCommand: stringify(readConfig(ctx, "wslPythonCommand")) || DEFAULT_SETTINGS.wslPythonCommand,
    sessdata: stringify(readConfig(ctx, "sessdata")) || DEFAULT_SETTINGS.sessdata,
    biliJct: stringify(readConfig(ctx, "biliJct")) || DEFAULT_SETTINGS.biliJct,
    sttProvider: stringify(readConfig(ctx, "sttProvider")) || DEFAULT_SETTINGS.sttProvider,
    whisperModel: stringify(readConfig(ctx, "whisperModel")) || DEFAULT_SETTINGS.whisperModel,
    whisperDevice: normalizeWhisperDevice(readConfig(ctx, "whisperDevice") ?? DEFAULT_SETTINGS.whisperDevice),
    whisperLanguage: stringify(readConfig(ctx, "whisperLanguage")) || DEFAULT_SETTINGS.whisperLanguage,
    preferredSubtitleLanguages,
    audioFormat: stringify(readConfig(ctx, "audioFormat")) || DEFAULT_SETTINGS.audioFormat,
    cookiesFile: normalizePath(stringify(readConfig(ctx, "cookiesFile")) || DEFAULT_SETTINGS.cookiesFile),
    cookieBrowser: normalizeCookieBrowser(readConfig(ctx, "cookieBrowser") ?? DEFAULT_SETTINGS.cookieBrowser),
    saveToWorkspace: booleanValue(readConfig(ctx, "saveToWorkspace"), DEFAULT_SETTINGS.saveToWorkspace),
    workspacePath: normalizePath(stringify(readConfig(ctx, "workspacePath")) || DEFAULT_SETTINGS.workspacePath),
    maxReturnedTranscriptChars: numberValue(
      readConfig(ctx, "maxReturnedTranscriptChars"),
      DEFAULT_SETTINGS.maxReturnedTranscriptChars,
    ),
  };
}

function stringify(value) {
  return typeof value === "string" ? value.trim() : "";
}

function booleanValue(value, fallback) {
  return typeof value === "boolean" ? value : fallback;
}

function numberValue(value, fallback) {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

function parseLanguages(value) {
  if (Array.isArray(value)) {
    return value.map(it => String(it).trim()).filter(Boolean);
  }
  return String(value ?? "")
    .split(",")
    .map(it => it.trim())
    .filter(Boolean);
}

function normalizeMode(value) {
  return ["auto", "native", "wsl"].includes(value) ? value : DEFAULT_SETTINGS.runtimeMode;
}

function normalizeWhisperDevice(value) {
  return ["auto", "cuda", "cpu"].includes(value) ? value : DEFAULT_SETTINGS.whisperDevice;
}

function normalizeCookieBrowser(value) {
  return ["auto", "edge", "chrome", "none"].includes(value) ? value : DEFAULT_SETTINGS.cookieBrowser;
}

function normalizePath(value) {
  if (!value) {
    return "";
  }
  return path.normalize(value);
}
