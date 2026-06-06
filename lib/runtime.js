import fs from "node:fs/promises";
import path from "node:path";
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import { BiliIntakeError } from "./errors.js";
import { CAPTURES_DIR, COLLECTOR_SCRIPT, PROVIDERS_CONFIG, REQUIREMENTS_FILE, REQUIRED_PYTHON_PACKAGES, RUNTIME_DIR } from "./constants.js";
import { ensureDir, fileExists } from "./utils.js";

const CURRENT_DIR = path.dirname(fileURLToPath(import.meta.url));
const DEFAULT_PLUGIN_DIR = path.resolve(CURRENT_DIR, "..");

/**
 * 准备 Python 运行时。
 *
 * 简化后的策略：
 * 1. 如果配置了 pythonPath，直接使用它
 * 2. 否则 fallback 到 virtualenv 模式（创建专属 venv）
 * 3. 检查依赖是否满足，不满足时输出需要安装的依赖列表
 */
export async function prepareRuntime(ctx, settings) {
  const pluginDir = ctx?.pluginDir ? path.resolve(ctx.pluginDir) : DEFAULT_PLUGIN_DIR;
  const dataDir = ctx?.dataDir ? path.resolve(ctx.dataDir) : path.join(pluginDir, ".data");
  const runtimeRoot = await ensureDir(path.join(dataDir, RUNTIME_DIR));
  const capturesRoot = await ensureDir(path.join(dataDir, CAPTURES_DIR));
  const requirementsPath = path.join(pluginDir, REQUIREMENTS_FILE);
  const collectorPath = path.join(pluginDir, COLLECTOR_SCRIPT);
  const providersConfigPath = path.join(pluginDir, PROVIDERS_CONFIG);

  // pythonPath 模式：直接使用指定 Python，不做依赖检查
  if (settings.pythonPath) {
    const pythonExe = settings.pythonPath;
    if (!(await fileExists(pythonExe))) {
      throw new BiliIntakeError(`指定 pythonPath 不存在: ${pythonExe}`, {
        code: "PYTHON_PATH_NOT_FOUND",
      });
    }
    return createDirectRuntime({
      pluginDir, dataDir, runtimeRoot, capturesRoot,
      requirementsPath, collectorPath, providersConfigPath,
      settings, mode: "native", selectedMode: "native",
      candidateModes: ["native"], fallbackUsed: false,
      venvDir: "",
    });
  }

  // 非 pythonPath 模式：自动创建 venv
  const candidateModes = resolveRuntimeModePlan(settings.runtimeMode);

  const baseRuntime = {
    pluginDir,
    dataDir,
    runtimeRoot,
    capturesRoot,
    requirementsPath,
    collectorPath,
    providersConfigPath,
    settings,
  };
  const failures = [];

  for (const mode of candidateModes) {
    const runtime = createRuntimeForMode(baseRuntime, mode, candidateModes);
    try {
      await ensureVenvAvailable(runtime);
      return runtime;
    } catch (error) {
      if (candidateModes.length === 1) {
        throw error;
      }
      failures.push(summarizeRuntimeFailure(mode, runtime, error));
    }
  }

  throw new BiliIntakeError("自动选择 Python 运行环境失败。", {
    code: "RUNTIME_SELECTION_FAILED",
    details: {
      requestedMode: settings.runtimeMode,
      attempts: failures,
    },
  });
}

function createDirectRuntime(base) {
  return {
    ...base,
    mode: "native",
    selectedMode: "native",
    candidateModes: ["native"],
    fallbackUsed: false,
  };
}

export function resolveRuntimeModePlan(configuredMode, platform = process.platform) {
  if (configuredMode === "native" || configuredMode === "wsl") {
    return [configuredMode];
  }
  return platform === "win32" ? ["native", "wsl"] : ["native"];
}

function createRuntimeForMode(baseRuntime, mode, candidateModes) {
  const runtimeRoot = baseRuntime.runtimeRoot;
  const venvDir = path.join(runtimeRoot, mode === "native" && process.platform === "win32" ? "venv-win" : `venv-${mode}`);
  return {
    ...baseRuntime,
    mode,
    venvDir,
    candidateModes: [...candidateModes],
    selectedMode: mode,
    fallbackUsed: mode !== candidateModes[0],
  };
}

function summarizeRuntimeFailure(mode, runtime, error) {
  const normalized = error instanceof BiliIntakeError
    ? {
        code: error.code || "RUNTIME_ERROR",
        message: error.message,
        details: error.details || null,
      }
    : {
        code: "RUNTIME_ERROR",
        message: error instanceof Error ? error.message : String(error),
        details: null,
      };
  return {
    mode,
    venvDir: runtime.venvDir,
    ...normalized,
  };
}

/**
 * 确保 Python 可用：
 * - 如果配置了 pythonPath，直接使用它
 * - 否则创建/使用 venv
 * - 检查必需的依赖
 */
async function ensureVenvAvailable(runtime) {
  if (runtime.settings.pythonPath) {
    // 用户指定了 Python 路径，直接使用，跳过依赖检查
    // 依赖检查由 spawn 权限问题暂时跳过，依赖由用户自行确保
    const pythonExe = runtime.settings.pythonPath;
    if (!(await fileExists(pythonExe))) {
      throw new BiliIntakeError(`指定 pythonPath 不存在: ${pythonExe}`, {
        code: "PYTHON_PATH_NOT_FOUND",
      });
    }
    return;
  }

  // 默认走 venv 模式
  if (runtime.mode === "wsl" && process.platform !== "win32") {
    throw new BiliIntakeError("当前进程不是 Windows，不能启用 WSL 运行模式。", { code: "INVALID_WSL_MODE" });
  }

  await ensureDir(runtime.venvDir);
  const venvReady = await isVenvReady(runtime);

  if (!venvReady) {
    const bootstrapPython = getBootstrapPython(runtime);
    await runCommand(runtime, [bootstrapPython, "-m", "venv", runtime.venvDir], {
      label: "create-venv",
      inputPaths: [runtime.venvDir],
    });
  }

  const pythonExe = getVenvPython(runtime);
  const missing = await checkDependencies(pythonExe);
  if (missing.length > 0) {
    // 尝试安装缺失依赖
    try {
      await runCommand(runtime, [pythonExe, "-m", "pip", "install", "--upgrade", "pip", "wheel", "setuptools"], {
        label: "pip-bootstrap",
        inputPaths: [pythonExe],
      });
      const depList = missing.filter(pkg => pkg !== "torch" && pkg !== "torchvision" && pkg !== "torchaudio");
      if (depList.length > 0) {
        await runCommand(runtime, [pythonExe, "-m", "pip", "install", ...depList], {
          label: "pip-install-deps",
          inputPaths: [pythonExe],
        });
      }
      // 检查安装是否成功
      const stillMissing = await checkDependencies(pythonExe);
      if (stillMissing.length > 0) {
        throw new BiliIntakeError(
          `依赖安装后仍然缺失: ${stillMissing.join(", ")}`,
          { code: "DEPENDENCIES_MISSING_AFTER_INSTALL", details: { missing: stillMissing, pythonExe } },
        );
      }
    } catch (error) {
      if (error instanceof BiliIntakeError) {
        throw error;
      }
      throw new BiliIntakeError(
        `安装 Python 依赖失败: ${missing.join(", ")}\n请手动运行: ${pythonExe} -m pip install ${missing.join(" ")}`,
        { code: "DEPENDENCIES_INSTALL_FAILED", details: { missing, pythonExe }, cause: error },
      );
    }
  }
}

async function isVenvReady(runtime) {
  return fileExists(getVenvPython(runtime));
}

function getBootstrapPython(runtime) {
  return runtime.mode === "wsl" ? runtime.settings.wslPythonCommand : runtime.settings.nativePythonCommand;
}

export function getVenvPython(runtime) {
  // 如果指定了 pythonPath，直接返回它
  if (runtime.settings.pythonPath) {
    return runtime.settings.pythonPath;
  }
  if (runtime.mode === "native") {
    if (process.platform === "win32") {
      return path.join(runtime.venvDir, "Scripts", "python.exe");
    }
    return path.join(runtime.venvDir, "bin", "python");
  }
  return path.join(runtime.venvDir, "bin", "python");
}

async function checkDependencies(pythonExe) {
  const missing = [];
  for (const pkg of REQUIRED_PYTHON_PACKAGES) {
    try {
      // 先用 import 检查包是否可用（更准确，适用于 py 包和命名不同的情况）
      const testCode = createImportTest(pkg);
      await runCommandSimple(pythonExe, ["-c", testCode], 30);
    } catch {
      missing.push(pkg);
    }
  }
  return missing;
}

function createImportTest(pkgName) {
  // 处理包名与 import 名不同的情况
  const importMap = {
    "openai-whisper": "whisper",
    "yt-dlp": "yt_dlp",
  };
  const importName = importMap[pkgName] || pkgName;
  return `import ${importName}`;
}

async function runCommandSimple(pythonExe, args, timeout = 30) {
  return new Promise((resolve, reject) => {
    const child = spawn(pythonExe, args, {
      stdio: ["ignore", "pipe", "pipe"],
      env: Object.assign({}, process.env, { PYTHONUTF8: "1" }),
      windowsHide: true,
      timeout: timeout * 1000,
    });
    let stderr = "";
    child.stderr.on("data", chunk => { stderr += String(chunk); });
    child.on("error", error => reject(error));
    child.on("close", code => {
      code === 0 ? resolve() : reject(new Error(`exit ${code}: ${stderr.slice(0, 200)}`));
    });
  });
}

export async function runCollector(runtime, payload) {
  const pythonExe = getVenvPython(runtime);

  // 如果设置了 sessdata，自动生成 cookies 文件
  if (runtime.settings.sessdata) {
    const cookiesPath = path.join(runtime.dataDir, "cookies.txt");
    const sessdata = runtime.settings.sessdata;
    let dedeUserId = "0";
    // 从 SESSDATA 提取用户 ID（格式：uid%2Ctimestamp%2Chash）
    const firstPart = sessdata.split("%2C")[0];
    if (firstPart && /^\d+$/.test(firstPart)) {
      dedeUserId = firstPart;
    }
    const biliJct = runtime.settings.biliJct || "";
    const expires = Math.floor(Date.now() / 1000) + 2592000; // 30 天后

    const lines = [
      "# Netscape HTTP Cookie File",
      "# Auto-generated by hanako-bilibili-intake",
      `.bilibili.com\tTRUE\t/\tTRUE\t${expires}\tSESSDATA\t${sessdata}`,
      `.bilibili.com\tTRUE\t/\tTRUE\t${expires}\tDedeUserID\t${dedeUserId}`,
    ];
    if (biliJct) {
      lines.push(`.bilibili.com\tTRUE\t/\tTRUE\t${expires}\tbili_jct\t${biliJct}`);
    }
    lines.push("");

    await fs.writeFile(cookiesPath, lines.join("\n"), "utf-8");
    runtime.settings.cookiesFile = cookiesPath;
  }

  const args = [
    pythonExe,
    runtime.collectorPath,
    "--source", payload.source,
    "--output-dir", payload.outputDir,
    "--audio-format", runtime.settings.audioFormat,
    "--whisper-model", payload.whisperModel || runtime.settings.whisperModel,
    "--whisper-device", payload.whisperDevice || runtime.settings.whisperDevice,
    "--return-text-limit", String(payload.returnTextLimit),
    "--stt-provider", payload.sttProvider || runtime.settings.sttProvider,
    "--providers-config", runtime.providersConfigPath,
    "--cookie-browser", runtime.settings.cookieBrowser || "auto",
  ];

  const whisperLanguage = payload.whisperLanguage ?? runtime.settings.whisperLanguage;
  if (whisperLanguage) {
    args.push("--whisper-language", whisperLanguage);
  }
  if (payload.forceTranscribe === true) {
    args.push("--force-transcribe");
  }
  if (payload.saveToWorkspace === true) {
    args.push("--save-to-workspace");
    if (runtime.settings.workspacePath) {
      args.push("--workspace-path", runtime.settings.workspacePath);
    }
  }
  for (const language of runtime.settings.preferredSubtitleLanguages) {
    args.push("--subtitle-language", language);
  }
  if (runtime.settings.cookiesFile) {
    args.push("--cookies-file", runtime.settings.cookiesFile);
  }
  if (payload.page && Number(payload.page) > 1) {
    args.push("--page", String(payload.page));
  }

  const inputPaths = [pythonExe, runtime.collectorPath, payload.outputDir];
  if (runtime.settings.cookiesFile) {
    inputPaths.push(runtime.settings.cookiesFile);
  }

  const result = await runCommand(runtime, args, {
    label: "collector",
    inputPaths,
  });

  try {
    return parseHelperJson(result.stdout);
  } catch (error) {
    throw new BiliIntakeError("Python helper 没有返回合法 JSON。", {
      code: "INVALID_HELPER_OUTPUT",
      details: {
        stdout: result.stdout,
        stderr: result.stderr,
      },
      cause: error,
    });
  }
}

export function parseHelperJson(stdout) {
  const normalized = String(stdout ?? "").trim();
  if (!normalized) {
    throw new Error("empty stdout");
  }

  try {
    return JSON.parse(normalized);
  } catch {
    // 处理 stdout 中有第三方库日志的情况
    const lines = normalized
      .split(/\r?\n/)
      .map(line => line.trim())
      .filter(Boolean);
    for (let index = lines.length - 1; index >= 0; index -= 1) {
      const candidate = lines.slice(index).join("\n").trim();
      if (!(candidate.startsWith("{") || candidate.startsWith("["))) {
        continue;
      }
      try {
        return JSON.parse(candidate);
      } catch {
        // continue
      }
    }
    throw new Error("no JSON payload found in stdout");
  }
}

async function runCommand(runtime, args, options = {}) {
  const { label = "command", inputPaths = [] } = options;
  if (runtime.mode === "wsl") {
    const translated = [];
    for (const arg of args) {
      translated.push(await maybeToWslPath(arg, inputPaths));
    }
    return spawnAndCollect("wsl.exe", ["-e", ...translated], label, runtime);
  }
  return spawnAndCollect(args[0], args.slice(1), label, runtime);
}

function spawnAndCollect(command, args, label, runtime) {
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, {
      stdio: ["ignore", "pipe", "pipe"],
      env: buildEnv(runtime),
      windowsHide: true,
    });
    let stdout = "";
    let stderr = "";

    child.stdout.on("data", chunk => {
      stdout += String(chunk);
    });
    child.stderr.on("data", chunk => {
      stderr += String(chunk);
    });
    child.on("error", error => {
      reject(new BiliIntakeError(`执行 ${label} 失败。`, {
        code: "SPAWN_FAILED",
        details: { command, args },
        cause: error,
      }));
    });
    child.on("close", code => {
      if (code === 0) {
        resolve({ stdout, stderr });
        return;
      }
      reject(new BiliIntakeError(`执行 ${label} 失败，退出码 ${code}。`, {
        code: "COMMAND_FAILED",
        details: { command, args, stdout, stderr, code },
      }));
    });
  });
}

function buildEnv(runtime) {
  const env = { ...process.env };
  env.PYTHONUTF8 = "1";
  env.PIP_DISABLE_PIP_VERSION_CHECK = "1";

  if (runtime.settings.pythonPath) {
    // pythonPath 模式下，模型缓存放在 conda 环境根目录下
    const condaRoot = path.dirname(path.resolve(runtime.settings.pythonPath));
    env.WHISPER_CACHE_DIR = path.join(condaRoot, "whisper_cache");
  } else if (runtime.mode === "native") {
    const cacheRoot = path.join(runtime.runtimeRoot, "cache");
    env.WHISPER_CACHE_DIR = path.join(cacheRoot, "whisper");
    if (process.platform !== "win32") {
      env.XDG_CACHE_HOME = cacheRoot;
    }
  }
  return env;
}

async function maybeToWslPath(value, candidates) {
  if (typeof value !== "string" || !value) {
    return value;
  }

  const matchedPath = candidates.find(candidate => candidate && path.normalize(candidate) === path.normalize(value));
  if (matchedPath) {
    return toWslPath(matchedPath);
  }
  if (/^[A-Za-z]:\\/.test(value) || /^[A-Za-z]:\//.test(value)) {
    return toWslPath(value);
  }
  return value;
}

async function toWslPath(inputPath) {
  return new Promise((resolve, reject) => {
    const child = spawn("wsl.exe", ["-e", "wslpath", "-a", inputPath], {
      stdio: ["ignore", "pipe", "pipe"],
      windowsHide: true,
    });
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", chunk => {
      stdout += String(chunk);
    });
    child.stderr.on("data", chunk => {
      stderr += String(chunk);
    });
    child.on("error", error => {
      reject(new BiliIntakeError("调用 wslpath 失败。", {
        code: "WSLPATH_FAILED",
        details: { inputPath },
        cause: error,
      }));
    });
    child.on("close", code => {
      if (code === 0) {
        resolve(stdout.trim());
        return;
      }
      reject(new BiliIntakeError(`wslpath 失败，退出码 ${code}。`, {
        code: "WSLPATH_FAILED",
        details: { inputPath, stderr, code },
      }));
    });
  });
}
