"""
STT Provider 统一调用模块

每 provider 一个函数，统一接口与调用方解耦。
新增 provider 只需：
  1. providers.json 加条目
  2. 本文件加一个 transcribe_xxx() 函数
  3. PROVIDERS 字典加一行
"""

from __future__ import annotations

import base64
import json
import mimetypes
import os
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable

try:
    import requests
except ImportError:
    requests = None  # type: ignore[assignment]

# ─── 常量 ─────────────────────────────────────────────
BASE64_LIMIT_BYTES = 10 * 1024 * 1024  # 10MB
OVERLAP_SEC = 2  # 切片前后各预留 2 秒重叠
OVERLAP_CHARS = 6  # 2 秒 ≈ 6 个中文字符（保守估计）


# ─── 统一入口 ─────────────────────────────────────────

def transcribe(
    audio_path: str,
    provider_name: str,
    providers_config: dict[str, Any],
    chunk_duration: int = 300,
    max_retries: int = 2,
    max_workers: int = 4,
    whisper_fallback_fn: Callable | None = None,
) -> str:
    """
    统一入口，按 provider 名称路由到对应实现。

    Args:
        audio_path: 音频文件路径。
        provider_name: provider 名称（如 "mimo"、"openai"）。
        providers_config: providers.json 的完整内容。
        chunk_duration: 每片时长（秒），默认 300（5 分钟）。
        max_retries: 单片重试次数，默认 2。
        max_workers: 并发上限，默认 4。
        whisper_fallback_fn: 可选的单片 Whisper 降级函数，接收音频路径返回文本。

    Returns:
        转写文本。

    Raises:
        ProviderError: 所有重试均失败且无降级逻辑时。
    """
    provider_fn = PROVIDERS.get(provider_name)
    if not provider_fn:
        raise ProviderError(f"未知 provider: {provider_name}")

    provider_cfg = providers_config.get("stt", {}).get(provider_name, {})
    if not provider_cfg.get("enabled", True):
        raise ProviderError(f"provider {provider_name} 已禁用")

    audio_path_obj = Path(audio_path)
    if not audio_path_obj.exists():
        raise ProviderError(f"音频文件不存在: {audio_path}")

    # 检查文件大小是否需要切片
    encoded_size = estimate_base64_size(audio_path_obj)
    if encoded_size <= BASE64_LIMIT_BYTES:
        # 单片直接调用
        return _transcribe_one_with_retry(
            audio_path_obj, provider_fn, provider_cfg, max_retries, whisper_fallback_fn
        )

    # 超过限制，切片并发
    return transcribe_chunked(
        audio_path_obj, provider_fn, provider_cfg, chunk_duration,
        max_retries, max_workers, whisper_fallback_fn,
    )


# ─── 切片与并发 ───────────────────────────────────────

def transcribe_chunked(
    audio_path: Path,
    provider_fn: Callable,
    config: dict[str, Any],
    chunk_duration: int,
    max_retries: int,
    max_workers: int,
    whisper_fallback_fn: Callable | None = None,
) -> str:
    """切片并发转写，结果去重叠后拼接。"""
    chunks = split_audio(audio_path, chunk_duration)
    if not chunks:
        raise ProviderError("音频切片失败")

    results: list[str | None] = [None] * len(chunks)

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {}
        for i, chunk_path in enumerate(chunks):
            futures[pool.submit(
                _transcribe_one_with_retry, chunk_path, provider_fn,
                config, max_retries, whisper_fallback_fn
            )] = i

        for future in as_completed(futures):
            idx = futures[future]
            try:
                results[idx] = future.result()
            except Exception as exc:
                idx_info = f"第 {idx + 1}/{len(chunks)} 片"
                raise ProviderError(f"{idx_info} 转写失败: {exc}") from exc

    # 清理临时切片
    for chunk in chunks:
        try:
            chunk.unlink(missing_ok=True)
        except Exception:
            pass

    return merge_chunk_results([r for r in results if r is not None])


def split_audio(audio_path: Path, chunk_duration: int) -> list[Path]:
    """用 ffmpeg 将音频按 chunk_duration 切片（每片前后各 OVERLAP_SEC 重叠）。"""
    total_duration = get_audio_duration(audio_path)
    if total_duration <= 0:
        raise ProviderError(f"无法获取音频时长: {audio_path}")

    num_chunks = max(1, int(total_duration / chunk_duration) + (1 if total_duration % chunk_duration > 0 else 0))
    chunk_dir = Path(tempfile.mkdtemp(prefix="bili_stt_chunk_"))
    chunks: list[Path] = []

    ext = audio_path.suffix.lower()
    if ext not in (".mp3", ".wav", ".m4a"):
        ext = ".mp3"

    for i in range(num_chunks):
        start = max(0, i * chunk_duration - OVERLAP_SEC)
        end = min(total_duration, (i + 1) * chunk_duration + OVERLAP_SEC)
        duration = end - start
        chunk_path = chunk_dir / f"chunk_{i:04d}{ext}"

        # 尝试用 -c copy 流复制（更快），如果不兼容则 fallback 到重编码
        result = subprocess.run(
            ["ffmpeg", "-y", "-i", str(audio_path),
             "-ss", str(start), "-t", str(duration),
             "-c", "copy", str(chunk_path)],
            capture_output=True, text=True, timeout=600,
        )
        if result.returncode != 0 or not chunk_path.exists() or chunk_path.stat().st_size == 0:
            # fallback: 重编码
            subprocess.run(
                ["ffmpeg", "-y", "-i", str(audio_path),
                 "-ss", str(start), "-t", str(duration),
                 "-acodec", "libmp3lame" if ext == ".mp3" else "aac",
                 "-b:a", "128k", str(chunk_path)],
                capture_output=True, text=True, timeout=600,
            )

        if chunk_path.exists() and chunk_path.stat().st_size > 0:
            chunks.append(chunk_path)

    return chunks


def merge_chunk_results(results: list[str]) -> str:
    """拼接转写结果，去除相邻片重叠区域的重复文本。"""
    if not results:
        return ""
    merged = results[0]
    for i in range(1, len(results)):
        current = results[i]
        # 去掉前一片末尾的重叠部分
        if len(merged) > OVERLAP_CHARS:
            merged = merged[:-OVERLAP_CHARS]
        merged += "\n" + current
    return merged


# ─── 重试逻辑 ────────────────────────────────────────

def _transcribe_one_with_retry(
    audio_path: Path,
    provider_fn: Callable,
    config: dict[str, Any],
    max_retries: int,
    whisper_fallback_fn: Callable | None = None,
) -> str:
    """尝试 provider 转写，失败后重试或降级到 Whisper。"""
    for attempt in range(max_retries + 1):
        try:
            return provider_fn(audio_path, config)
        except ProviderRetryableError as e:
            if attempt >= max_retries:
                if whisper_fallback_fn:
                    sys.stderr.write(f"[stt] {audio_path.name} provider 重试耗尽，降级 Whisper\n")
                    return whisper_fallback_fn(str(audio_path))
                raise ProviderError(f"重试耗尽: {e}") from e
            wait = 2 ** attempt  # 指数退避: 1s, 2s, 4s
            sys.stderr.write(f"[stt] 重试 {attempt + 1}/{max_retries} (等待 {wait}s): {e}\n")
            time.sleep(wait)
        except ProviderFatalError as e:
            # 不可重试错误，直接降级
            if whisper_fallback_fn:
                sys.stderr.write(f"[stt] {audio_path.name} provider 致命错误，降级 Whisper: {e}\n")
                return whisper_fallback_fn(str(audio_path))
            raise ProviderError(f"致命错误且无降级: {e}") from e

    error_msg = f"{audio_path.name} provider 调用全部失败"
    if whisper_fallback_fn:
        sys.stderr.write(f"[stt] {error_msg}，降级 Whisper\n")
        return whisper_fallback_fn(str(audio_path))
    raise ProviderError(error_msg)


# ─── MiMo ASR 实现 ───────────────────────────────────

def transcribe_mimo(audio_path: Path, config: dict[str, Any]) -> str:
    """调用 MiMo ASR API 转写音频。"""
    if requests is None:
        raise ProviderFatalError("缺少 requests 库，无法调用 MiMo ASR")

    api_base = config.get("api_base", "https://api.xiaomimimo.com/v1")
    api_key = config.get("api_key", "")
    model = config.get("model", "mimo-v2.5-asr")
    language = config.get("language", "auto")

    if not api_key:
        raise ProviderFatalError("MiMo API key 未配置")

    # 读取音频并转为 base64
    audio_bytes = audio_path.read_bytes()
    mime_type, _ = mimetypes.guess_type(str(audio_path))
    if not mime_type or mime_type == "application/octet-stream":
        ext = audio_path.suffix.lower()
        if ext == ".mp3":
            mime_type = "audio/mpeg"
        elif ext == ".wav":
            mime_type = "audio/wav"
        elif ext == ".m4a":
            mime_type = "audio/mp4"
        else:
            mime_type = "audio/mpeg"

    b64_data = base64.b64encode(audio_bytes).decode("utf-8")
    data_uri = f"data:{mime_type};base64,{b64_data}"

    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_audio",
                        "input_audio": {
                            "data": data_uri,
                        },
                    }
                ],
            }
        ],
        "asr_options": {
            "language": language,
        },
    }

    headers = {
        "api-key": api_key,
        "Content-Type": "application/json",
    }

    url = f"{api_base.rstrip('/')}/chat/completions"

    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=600)
    except requests.exceptions.Timeout:
        raise ProviderRetryableError("MiMo ASR 请求超时")
    except requests.exceptions.ConnectionError as e:
        raise ProviderRetryableError(f"MiMo ASR 连接失败: {e}")
    except requests.exceptions.RequestException as e:
        raise ProviderRetryableError(f"MiMo ASR 请求异常: {e}")

    status = resp.status_code
    if status == 401:
        raise ProviderFatalError("MiMo API key 无效 (401)")
    if status == 429:
        raise ProviderRetryableError("MiMo 频率超限 (429)")
    if status >= 500:
        raise ProviderRetryableError(f"MiMo 服务端错误 ({status})")
    if status != 200:
        raise ProviderRetryableError(f"MiMo 返回非预期状态码: {status}")

    try:
        data = resp.json()
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, json.JSONDecodeError) as e:
        raise ProviderRetryableError(f"MiMo 响应解析失败: {e}")


# ─── Provider 注册表 ─────────────────────────────────

PROVIDERS: dict[str, Callable] = {
    "mimo": transcribe_mimo,
    # "openai": transcribe_openai,  # 预留
}


# ─── 辅助函数 ─────────────────────────────────────────

def estimate_base64_size(audio_path: Path) -> int:
    """估算文件的 base64 编码后大小（字节）。"""
    size = audio_path.stat().st_size
    return int(size * 4 / 3) + 4  # base64 膨胀系数 ≈ 4/3


def get_audio_duration(audio_path: Path) -> float:
    """用 ffprobe 获取音频时长（秒）。"""
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(audio_path)],
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode == 0 and result.stdout.strip():
        try:
            return float(result.stdout.strip())
        except ValueError:
            pass
    return 0.0


# ─── 错误类型 ────────────────────────────────────────

class ProviderError(RuntimeError):
    """STT provider 通用错误。"""
    pass


class ProviderRetryableError(ProviderError):
    """可重试的临时错误（网络超时、429、500 等）。"""
    pass


class ProviderFatalError(ProviderError):
    """不可重试的致命错误（API key 无效、配置错误等）。"""
    pass
