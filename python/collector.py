#!/usr/bin/env python3
"""
Bilibili 视频采集器 - 四层字幕降级策略

第1层：CC 字幕（人工上传，100% 准确）
第2层：AI 字幕（B站自动生成，ai-zh/ai-en 等，需 cookie）
第3层：MiMo ASR（云端 API，通过 stt_providers 调用）
第4层：Whisper 本地转写（兜底）

forceTranscribe: true 时跳过第1-3层，直接从第4层开始。
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

import yt_dlp

try:
    from stt_providers import transcribe as stt_transcribe
    from stt_providers import ProviderError, ProviderFatalError, ProviderRetryableError
    STT_AVAILABLE = True
except ImportError:
    STT_AVAILABLE = False

PLUGIN_ROOT = Path(__file__).resolve().parent.parent


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Bilibili 视频采集器（四层降级）")
    parser.add_argument("--source", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--audio-format", default="mp3")
    parser.add_argument("--whisper-model", default="auto")
    parser.add_argument("--whisper-device", default="auto")
    parser.add_argument("--whisper-language", default="")
    parser.add_argument("--subtitle-language", action="append", dest="subtitle_languages", default=[])
    parser.add_argument("--cookies-file", default="")
    parser.add_argument("--page", type=int, default=0)
    parser.add_argument("--force-transcribe", action="store_true")
    parser.add_argument("--return-text-limit", type=int, default=12000)
    parser.add_argument("--stt-provider", default="mimo")
    parser.add_argument("--providers-config", default="")
    parser.add_argument("--save-to-workspace", action="store_true")
    parser.add_argument("--workspace-path", default="")
    parser.add_argument("--cookie-browser", default="auto")
    return parser.parse_args()


def log(message: str) -> None:
    print(f"[collector] {message}", file=sys.stderr, flush=True)


class QuietLogger:
    def debug(self, msg: str) -> None:
        if msg and "[debug]" in msg.lower():
            log(msg)

    def warning(self, msg: str) -> None:
        if msg:
            log(msg)

    def error(self, msg: str) -> None:
        if msg:
            log(msg)


class CollectorError(RuntimeError):
    pass


# ─── 工具函数 ────────────────────────────────────────

def normalize_source(source: str, page: int) -> str:
    raw = source.strip()
    if re.fullmatch(r"BV[0-9A-Za-z]+", raw, flags=re.IGNORECASE):
        url = f"https://www.bilibili.com/video/{raw}"
    elif re.fullmatch(r"av\d+", raw, flags=re.IGNORECASE):
        url = f"https://www.bilibili.com/video/{raw}"
    else:
        url = raw

    if page and page > 1:
        separator = "&" if "?" in url else "?"
        if re.search(r"([?&])p=\d+", url):
            url = re.sub(r"([?&])p=\d+", rf"\1p={page}", url)
        else:
            url = f"{url}{separator}p={page}"
    return url


def build_common_ydl_opts(cookies_file: str = "", cookie_browser: str = "") -> dict[str, Any]:
    opts: dict[str, Any] = {
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "logger": QuietLogger(),
        "restrictfilenames": False,
        "consoletitle": False,
        "http_headers": {
            "Referer": "https://www.bilibili.com",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        },
    }
    if cookies_file:
        opts["cookiefile"] = cookies_file
    return opts


def write_json(file_path: Path, payload: Any) -> None:
    file_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )


def truncate_text(text: str, max_length: int) -> str:
    clean = text.strip()
    if max_length <= 0 or len(clean) <= max_length:
        return clean
    return clean[: max(0, max_length - 1)].rstrip() + "…"


def normalize_plain_text(raw: str) -> str:
    lines: list[str] = []
    previous = ""
    for line in str(raw).splitlines():
        candidate = line.replace("\\N", " ")
        candidate = re.sub(r"<[^>]+>", " ", candidate)
        candidate = re.sub(r"\{[^}]+\}", " ", candidate)
        candidate = re.sub(r"\s+", " ", candidate).strip()
        if not candidate:
            continue
        if candidate == previous:
            continue
        previous = candidate
        lines.append(candidate)
    return "\n".join(lines)


def sanitize_filename(name: str, max_len: int = 60) -> str:
    """去除文件名中的非法字符，截断至 max_len。"""
    cleaned = re.sub(r'[\\/:*?"<>|]', "", name)
    cleaned = re.sub(r"\s+", "_", cleaned).strip()
    if not cleaned:
        cleaned = "untitled"
    return cleaned[:max_len]


# ─── 主函数 ──────────────────────────────────────────

def main() -> int:
    args = parse_args()
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    source = normalize_source(args.source, args.page)
    fallback_log: list[dict[str, Any]] = []

    metadata_path = output_dir / "metadata.json"
    raw_info_path = output_dir / "raw_info.json"
    audio_streams_path = output_dir / "audio_streams.json"
    transcript_text_path = output_dir / "transcript.txt"
    result_path = output_dir / "result.json"

    # ── 获取元信息 ─────────────────────────────────────
    info = extract_info(source, args.cookies_file, args.cookie_browser)
    metadata = build_metadata(info, source)
    audio_streams = build_audio_streams(info)
    write_json(metadata_path, metadata)
    write_json(raw_info_path, info)
    write_json(audio_streams_path, audio_streams)

    duration = metadata.get("duration") or 0
    title = metadata.get("title") or ""
    description = metadata.get("description") or ""
    uploader = metadata.get("uploader") or ""

    # ── 第0步：forceTranscribe 检查 ────────────────────
    if args.force_transcribe:
        log("forceTranscribe 开启，跳过所有字幕和云端 ASR，直接 Whisper 本地转写。")
        fallback_log.append({
            "layer": "CC字幕",
            "status": "skipped",
            "reason": "forceTranscribe 开启",
        })
        fallback_log.append({
            "layer": "AI字幕",
            "status": "skipped",
            "reason": "forceTranscribe 开启",
        })
        fallback_log.append({
            "layer": f"云端 STT ({args.stt_provider})",
            "status": "skipped",
            "reason": "forceTranscribe 开启",
        })
    else:
        # ── 第1层：CC 字幕 ───────────────────────────
        download_subtitles(source, output_dir, args.subtitle_languages, args.cookies_file, args.cookie_browser)
        subtitle_files = list_cc_subtitle_files(output_dir)

        cc_text = ""
        if subtitle_files:
            cc_text = choose_subtitle_text(subtitle_files, args.subtitle_languages)
            if cc_text and len(cc_text.strip()) > 20:
                log(f"第1层：CC 字幕成功（{len(cc_text)} 字）")
                fallback_log.append({
                    "layer": "CC字幕",
                    "status": "success",
                    "detail": f"下载成功，{len(cc_text)} 字",
                })
                fallback_log.append({
                    "layer": "AI字幕",
                    "status": "skipped",
                    "reason": "前一层已成功",
                })
                fallback_log.append({
                    "layer": f"云端 STT ({args.stt_provider})",
                    "status": "skipped",
                    "reason": "前一层已成功",
                })
                fallback_log.append({
                    "layer": "Whisper",
                    "status": "skipped",
                    "reason": "前一层已成功",
                })
                finalize_and_exit(cc_text, metadata_path, raw_info_path, audio_streams_path,
                                  transcript_text_path, result_path, output_dir, source, metadata,
                                  args, fallback_log, "CC字幕", None, title, description, uploader, duration)
                return 0
            else:
                log("第1层：CC 字幕为空或不可用，降级")
                fallback_log.append({
                    "layer": "CC字幕",
                    "status": "failed",
                    "reason": "字幕为空" if not cc_text else f"内容过短（{len(cc_text.strip())} 字）",
                })
        else:
            log("第1层：无可用 CC 字幕")
            fallback_log.append({
                "layer": "CC字幕",
                "status": "skipped",
                "reason": "无可用CC字幕",
            })

        # ── 第2层：AI 字幕 ───────────────────────────
        # 先检查第一次下载有无 AI 字幕文件
        ai_subtitle_text = find_ai_subtitle_text(output_dir, args.subtitle_languages)
        if ai_subtitle_text and len(ai_subtitle_text.strip()) > 20:
            ai_lang = detect_ai_subtitle_language(output_dir)
            log(f"第2层：AI 字幕成功（已下载文件，{len(ai_subtitle_text)} 字）")
        else:
            # 没有或为空，重新下载（加 cookie）
            ai_subtitle_text = download_ai_subtitles(source, output_dir, args.cookies_file, args.cookie_browser)
            if ai_subtitle_text and len(ai_subtitle_text.strip()) > 20:
                ai_lang = detect_ai_subtitle_language(output_dir)
                log(f"第2层：AI 字幕成功（{len(ai_subtitle_text)} 字）")
            else:
                ai_subtitle_text = ""

        if ai_subtitle_text:
            ai_lang = detect_ai_subtitle_language(output_dir)
            fallback_log.append({
                "layer": f"AI字幕({ai_lang})",
                "status": "success",
                "detail": f"下载成功，{len(ai_subtitle_text)} 字",
            })
            fallback_log.append({
                "layer": f"云端 STT ({args.stt_provider})",
                "status": "skipped",
                "reason": "前一层已成功",
            })
            fallback_log.append({
                "layer": "Whisper",
                "status": "skipped",
                "reason": "前一层已成功",
            })
            finalize_and_exit(ai_subtitle_text, metadata_path, raw_info_path, audio_streams_path,
                              transcript_text_path, result_path, output_dir, source, metadata,
                              args, fallback_log, f"AI字幕({ai_lang})", None,
                              title, description, uploader, duration)
            return 0
        else:
            reason = "AI 字幕空" if not ai_subtitle_text else f"AI 字幕过短（{len(ai_subtitle_text.strip())} 字）"
            log(f"第2层：{reason}")
            fallback_log.append({
                "layer": "AI字幕",
                "status": "failed",
                "reason": reason,
            })
    # ── 第3层 / 第4层 需要音频 ───────────────────────
    audio_path = download_audio(source, output_dir, args.audio_format, args.cookies_file)
    final_text = ""
    transcript_source = ""
    transcript_device = None

    # ── 第3层：云端 STT ─────────────────────────────
    if not args.force_transcribe and STT_AVAILABLE:
        providers_config_path = Path(args.providers_config) if args.providers_config else (PLUGIN_ROOT / "python" / "providers.json")
        try:
            if providers_config_path.exists():
                providers_config = json.loads(providers_config_path.read_text(encoding="utf-8"))
                stt_cfg = providers_config.get("stt", {}).get(args.stt_provider, {})
                if stt_cfg.get("enabled", True) and stt_cfg.get("api_key", ""):
                    log(f"第3层：尝试 {args.stt_provider} ASR...")
                    stt_result = stt_transcribe(
                        str(audio_path),
                        args.stt_provider,
                        providers_config,
                        chunk_duration=300,
                        max_retries=2,
                        max_workers=4,
                    )
                    if stt_result and len(stt_result.strip()) > 20:
                        log(f"第3层：{args.stt_provider} ASR 成功（{len(stt_result)} 字）")
                        fallback_log.append({
                            "layer": f"云端 STT ({args.stt_provider})",
                            "status": "success",
                            "detail": f"转写成功，{len(stt_result)} 字",
                        })
                        fallback_log.append({
                            "layer": "Whisper",
                            "status": "skipped",
                            "reason": "前一层已成功",
                        })
                        finalize_and_exit(stt_result, metadata_path, raw_info_path, audio_streams_path,
                                          transcript_text_path, result_path, output_dir, source, metadata,
                                          args, fallback_log, f"{args.stt_provider} ASR", transcript_device,
                                          title, description, uploader, duration)
                        return 0
                    else:
                        log(f"第3层：{args.stt_provider} ASR 结果为空或过短")
                        fallback_log.append({
                            "layer": f"云端 STT ({args.stt_provider})",
                            "status": "failed",
                            "reason": "转写结果为空或过短",
                        })
                else:
                    log(f"第3层：{args.stt_provider} 未配置或已禁用，跳过")
                    fallback_log.append({
                        "layer": f"云端 STT ({args.stt_provider})",
                        "status": "skipped",
                        "reason": "未配置 API key 或已禁用",
                    })
            else:
                log(f"第3层：providers.json 不存在，跳过云端 STT")
                fallback_log.append({
                    "layer": f"云端 STT ({args.stt_provider})",
                    "status": "skipped",
                    "reason": "providers.json 不存在",
                })
        except (ProviderFatalError, ProviderRetryableError, ProviderError) as e:
            log(f"第3层：{args.stt_provider} ASR 失败: {e}")
            fallback_log.append({
                "layer": f"云端 STT ({args.stt_provider})",
                "status": "failed",
                "reason": str(e),
            })
        except Exception as e:
            log(f"第3层：{args.stt_provider} ASR 异常: {e}")
            fallback_log.append({
                "layer": f"云端 STT ({args.stt_provider})",
                "status": "failed",
                "reason": str(e),
            })
    else:
        reason = "forceTranscribe 开启" if args.force_transcribe else "stt_providers 模块不可用"
        log(f"第3层：跳过云端 STT（{reason}）")
        fallback_log.append({
            "layer": f"云端 STT ({args.stt_provider})",
            "status": "skipped",
            "reason": reason,
        })

    # ── 第4层：Whisper 本地转写 ──────────────────────
    log("第4层：Whisper 本地转写兜底")
    whisper_model = select_whisper_model(args.whisper_model, duration)
    whisper_device = normalize_whisper_device(args.whisper_device)
    final_text, transcript_device = transcribe_with_whisper(
        audio_path, whisper_model, args.whisper_language, whisper_device,
    )
    transcript_source = f"Whisper {whisper_model}"
    if transcript_device == "cuda":
        transcript_source += "(GPU加速)"

    log(f"第4层：Whisper {whisper_model} 完成（{len(final_text)} 字）")
    fallback_log.append({
        "layer": f"Whisper({whisper_model})",
        "status": "success",
        "detail": f"转写完成，{len(final_text)} 字",
    })

    finalize_and_exit(final_text, metadata_path, raw_info_path, audio_streams_path,
                      transcript_text_path, result_path, output_dir, source, metadata,
                      args, fallback_log, transcript_source, transcript_device,
                      title, description, uploader, duration)
    return 0


# ─── 单元函数 ────────────────────────────────────────

def extract_info(source: str, cookies_file: str, cookie_browser: str = "") -> dict[str, Any]:
    """提取视频元信息。"""
    opts = build_common_ydl_opts(cookies_file, cookie_browser)
    apply_cookie_browser(opts, cookie_browser)
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(source, download=False)
    if not isinstance(info, dict):
        raise CollectorError("yt-dlp 返回的元信息不是对象。")
    return info


def apply_cookie_browser(opts: dict[str, Any], cookie_browser: str) -> None:
    """根据 cookie_browser 配置应用浏览器 cookie。"""
    if not cookie_browser or cookie_browser == "none":
        return

    browsers = []
    if cookie_browser == "auto":
        browsers = ["edge", "chrome"]
    else:
        browsers = [cookie_browser]

    for browser in browsers:
        try:
            opts["cookies_from_browser"] = browser
            log(f"尝试使用 {browser} 浏览器 cookie")
            return
        except Exception:
            continue

    log("无法获取浏览器 cookie，AI 字幕可能不可用")


def build_metadata(info: dict[str, Any], source: str) -> dict[str, Any]:
    return {
        "id": info.get("id"),
        "title": info.get("title"),
        "description": info.get("description") or "",
        "uploader": info.get("uploader") or info.get("channel") or info.get("uploader_id") or "",
        "channel": info.get("channel") or "",
        "duration": info.get("duration"),
        "webpageUrl": info.get("webpage_url") or source,
        "originalUrl": source,
        "thumbnail": info.get("thumbnail") or "",
        "tags": info.get("tags") or [],
        "uploadDate": info.get("upload_date") or "",
        "viewCount": info.get("view_count"),
        "likeCount": info.get("like_count"),
        "commentCount": info.get("comment_count"),
        "subtitleLanguages": sorted(list((info.get("subtitles") or {}).keys())),
        "automaticCaptionLanguages": sorted(list((info.get("automatic_captions") or {}).keys())),
    }


def build_audio_streams(info: dict[str, Any]) -> list[dict[str, Any]]:
    formats = info.get("formats") or []
    audio_only: list[dict[str, Any]] = []
    with_audio: list[dict[str, Any]] = []
    for fmt in formats:
        if not isinstance(fmt, dict):
            continue
        if not fmt.get("url"):
            continue
        acodec = fmt.get("acodec")
        vcodec = fmt.get("vcodec")
        if not acodec or acodec == "none":
            continue
        entry = {
            "format_id": fmt.get("format_id"),
            "format_note": fmt.get("format_note"),
            "ext": fmt.get("ext"),
            "audio_ext": fmt.get("audio_ext"),
            "protocol": fmt.get("protocol"),
            "url": fmt.get("url"),
            "abr": fmt.get("abr"),
            "asr": fmt.get("asr"),
            "tbr": fmt.get("tbr"),
            "filesize": fmt.get("filesize") or fmt.get("filesize_approx"),
            "language": fmt.get("language"),
            "acodec": acodec,
            "vcodec": vcodec,
        }
        with_audio.append(entry)
        if not vcodec or vcodec == "none" or str(fmt.get("resolution") or "").lower() == "audio only":
            audio_only.append(entry)

    target = audio_only or with_audio
    target.sort(
        key=lambda item: ((item.get("abr") or 0), (item.get("tbr") or 0), (item.get("filesize") or 0)),
        reverse=True,
    )
    return target


# ─── 字幕相关 ─────────────────────────────────────────

def download_subtitles(
    source: str, output_dir: Path, subtitle_languages: list[str],
    cookies_file: str, cookie_browser: str = "",
) -> None:
    """下载所有可用字幕（CC + AI）。"""
    opts = build_common_ydl_opts(cookies_file, cookie_browser)
    apply_cookie_browser(opts, cookie_browser)
    opts.update({
        "skip_download": True,
        "writesubtitles": True,
        "writeautomaticsub": True,
        "subtitleslangs": subtitle_languages or ["all"],
        "outtmpl": {"default": str(output_dir / "subtitle.%(ext)s")},
    })
    with yt_dlp.YoutubeDL(opts) as ydl:
        try:
            ydl.download([source])
        except Exception as e:
            log(f"字幕下载（含 cookie）失败: {e}")
            # 无 cookie 重试
            opts2 = build_common_ydl_opts("")
            opts2.update({
                "skip_download": True,
                "writesubtitles": True,
                "writeautomaticsub": True,
                "subtitleslangs": subtitle_languages or ["all"],
                "outtmpl": {"default": str(output_dir / "subtitle.%(ext)s")},
            })
            try:
                with yt_dlp.YoutubeDL(opts2) as ydl2:
                    ydl2.download([source])
            except Exception:
                pass


def list_cc_subtitle_files(output_dir: Path) -> list[Path]:
    """列出 output_dir 中的人工字幕文件（排除 ai- 和 danmaku）。"""
    files = sorted(output_dir.glob("subtitle.*"))
    result = []
    for p in files:
        if p.is_file() and p.suffix.lower() in {".srt", ".vtt", ".ass", ".ssa", ".lrc", ".json", ".json3", ".srv3", ".ttml", ".xml"}:
            name = p.name.lower()
            if "ai-" not in name and "danmaku" not in name:
                result.append(p)
    return result


def find_ai_subtitle_text(output_dir: Path, preferred_languages: list[str]) -> str:
    """从已下载的字幕文件中查找 AI 字幕文本。"""
    ai_patterns = ["ai-zh", "ai-en", "ai-ja", "ai-kr", "ai-th", "ai-id", "ai-vi"]
    ai_files: list[Path] = []
    for p in output_dir.iterdir():
        if p.is_file() and p.suffix.lower() in {'.srt', '.vtt', '.ass', '.ssa', '.lrc', '.json', '.json3', '.srv3', '.ttml', '.xml'}:
            for pat in ai_patterns:
                if pat in p.name.lower():
                    ai_files.append(p)
                    break
    if ai_files:
        return choose_subtitle_text(ai_files, preferred_languages)
    return ""


def download_ai_subtitles(source: str, output_dir: Path, cookies_file: str = "", cookie_browser: str = "") -> str:
    """专门下载 AI 字幕，返回纯文本。"""
    ai_langs = ["ai-zh", "ai-en", "ai-ja"]
    for lang in ai_langs:
        opts = build_common_ydl_opts(cookies_file, cookie_browser)
        apply_cookie_browser(opts, cookie_browser)
        opts.update({
            "skip_download": True,
            "writesubtitles": True,
            "writeautomaticsub": True,
            "subtitleslangs": [lang],
            "outtmpl": {"default": str(output_dir / f"subtitle.%(ext)s")},
        })
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([source])
            # 检查是否生成了 AI 字幕文件
            ai_files = sorted(output_dir.glob(f"*.{lang}.*"))
            if ai_files:
                text = choose_subtitle_text(ai_files, [lang])
                if text and len(text.strip()) > 20:
                    return text
        except Exception:
            continue
    return ""


def detect_ai_subtitle_language(output_dir: Path) -> str:
    """检测 output_dir 中 AI 字幕的语言标识。"""
    for p in output_dir.iterdir():
        name = p.name.lower()
        if "ai-zh" in name:
            return "ai-zh"
        if "ai-en" in name:
            return "ai-en"
        if "ai-ja" in name:
            return "ai-ja"
        if "ai-" in name:
            import re as _re
            m = _re.search(r"ai-[a-z]{2}", name)
            if m:
                return m.group()
    return "ai-zh"


def subtitle_priority_key(path: Path, preferred_languages: list[str]) -> tuple[int, int, str]:
    name = path.name.lower()
    language_score = len(preferred_languages) + 1
    for index, language in enumerate(preferred_languages):
        token = language.lower()
        if f".{token}." in name or name.startswith(f"{token}.") or token in name:
            language_score = index
            break
    extension_order = {
        ".srt": 0,
        ".vtt": 1,
        ".json": 2,
        ".json3": 3,
        ".srv3": 4,
        ".ass": 5,
        ".ssa": 6,
        ".lrc": 7,
        ".xml": 8,
        ".ttml": 9,
    }
    return (language_score, extension_order.get(path.suffix.lower(), 99), name)


def choose_subtitle_text(subtitle_files: list[Path], preferred_languages: list[str]) -> str:
    ranked = sorted(subtitle_files, key=lambda p: subtitle_priority_key(p, preferred_languages))
    for subtitle_file in ranked:
        text = parse_subtitle_file(subtitle_file)
        if text and len(text.strip()) > 20:
            return text
    return ""


def parse_subtitle_file(file_path: Path) -> str:
    suffix = file_path.suffix.lower()
    text = file_path.read_text(encoding="utf-8", errors="ignore")
    if suffix in {".json", ".json3", ".srv3"}:
        return parse_json_subtitle_text(text)
    if suffix in {".ass", ".ssa"}:
        return parse_ass_text(text)
    if suffix in {".xml", ".ttml"}:
        return parse_xml_caption_text(text)
    return strip_timed_text(text)


def parse_json_subtitle_text(raw: str) -> str:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return strip_timed_text(raw)

    lines: list[str] = []
    if isinstance(data, dict):
        body = data.get("body")
        if isinstance(body, list):
            for item in body:
                if isinstance(item, dict):
                    lines.append(str(item.get("content") or item.get("text") or ""))
        events = data.get("events")
        if isinstance(events, list):
            for event in events:
                if not isinstance(event, dict):
                    continue
                segs = event.get("segs") or []
                for seg in segs:
                    if isinstance(seg, dict):
                        lines.append(str(seg.get("utf8") or seg.get("text") or ""))
        segments = data.get("segments")
        if isinstance(segments, list):
            for item in segments:
                if isinstance(item, dict):
                    lines.append(str(item.get("text") or item.get("content") or ""))
    return normalize_plain_text("\n".join(lines))


def parse_ass_text(raw: str) -> str:
    lines: list[str] = []
    for line in raw.splitlines():
        if not line.startswith("Dialogue:"):
            continue
        parts = line.split(",", 9)
        if len(parts) == 10:
            lines.append(parts[-1])
    return normalize_plain_text("\n".join(lines))


def parse_xml_caption_text(raw: str) -> str:
    matches = re.findall(r">([^<]+)<", raw)
    return normalize_plain_text("\n".join(matches))


def strip_timed_text(raw: str) -> str:
    cleaned_lines: list[str] = []
    for line in raw.splitlines():
        candidate = line.strip().replace("\ufeff", "")
        if not candidate:
            continue
        if candidate.upper() in {"WEBVTT", "STYLE", "NOTE"}:
            continue
        if re.fullmatch(r"\d+", candidate):
            continue
        if re.search(r"\d{1,2}:\d{2}:\d{2}[\.,]\d{2,3}\s+-->\s+\d{1,2}:\d{2}:\d{2}[\.,]\d{2,3}", candidate):
            continue
        if re.search(r"\d{1,2}:\d{2}[\.,]\d{2,3}\s+-->\s+\d{1,2}:\d{2}[\.,]\d{2,3}", candidate):
            continue
        if candidate.startswith(("Kind:", "Language:", "X-TIMESTAMP-MAP")):
            continue
        cleaned_lines.append(candidate)
    return normalize_plain_text("\n".join(cleaned_lines))


# ─── 音频下载 ─────────────────────────────────────────

def download_audio(source: str, output_dir: Path, audio_format: str, cookies_file: str) -> Path:
    opts = build_common_ydl_opts(cookies_file)
    opts.update({
        "format": "bestaudio/best",
        "outtmpl": {"default": str(output_dir / "audio.%(ext)s")},
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": audio_format,
                "preferredquality": "0",
            }
        ],
    })
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([source])

    direct_target = output_dir / f"audio.{audio_format}"
    if direct_target.exists():
        return direct_target.resolve()

    candidates = sorted(output_dir.glob("audio.*"))
    for candidate in candidates:
        if candidate.is_file() and candidate.name != "audio_streams.json":
            return candidate.resolve()
    raise CollectorError("音频下载完成后没有找到导出的音频文件。")


# ─── Whisper 转写 ─────────────────────────────────────

def select_whisper_model(configured_model: str, duration_sec: int) -> str:
    """智能选择 Whisper 模型。"""
    if configured_model and configured_model != "auto":
        return configured_model

    gpu_info = detect_gpu_info()

    if gpu_info["has_cuda"]:
        vram_mb = gpu_info.get("vram_mb", 0)
        if vram_mb >= 6144:
            log("GPU 显存 >= 6GB → 使用 medium 模型")
            return "medium"
        else:
            log("GPU 存在但显存 < 6GB → 使用 small 模型")
            return "small"
    else:
        if duration_sec and duration_sec > 1800:
            log("无 GPU 且视频 > 30 分钟 → 使用 tiny 模型")
            return "tiny"
        else:
            log("无 GPU → 使用 base 模型")
            return "base"


def detect_gpu_info() -> dict[str, Any]:
    """检测 GPU 信息。"""
    result = {"has_cuda": False, "vram_mb": 0, "gpu_name": ""}

    # 用 nvidia-smi 检测
    smi = shutil.which("nvidia-smi")
    if smi:
        try:
            out = subprocess.run(
                [smi, "-L"], capture_output=True, text=True, timeout=5, check=False,
            )
            if out.returncode == 0 and out.stdout.strip():
                result["gpu_name"] = out.stdout.strip()
                # 查询显存
                vram_out = subprocess.run(
                    [smi, "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
                    capture_output=True, text=True, timeout=5, check=False,
                )
                if vram_out.returncode == 0 and vram_out.stdout.strip():
                    try:
                        result["vram_mb"] = int(vram_out.stdout.strip().split("\n")[0].strip())
                    except (ValueError, IndexError):
                        pass
                result["has_cuda"] = True
                return result
        except (OSError, subprocess.TimeoutExpired):
            pass

    # 用 torch 检测
    try:
        import torch
        if torch.cuda.is_available():
            result["has_cuda"] = True
            try:
                result["vram_mb"] = int(torch.cuda.get_device_properties(0).total_mem / 1024 / 1024)
            except Exception:
                pass
            return result
    except ImportError:
        pass

    return result


def normalize_whisper_device(device_preference: str) -> str:
    normalized = str(device_preference or "").strip().lower()
    if normalized in {"cuda", "cpu"}:
        return normalized
    return "auto"


def transcribe_with_whisper(
    audio_path: Path, model_name: str, language: str, device_preference: str,
    download_root: str | None = None,
) -> tuple[str, str]:
    """运行 Whisper 转写，返回 (文本, 设备)。"""
    try:
        import whisper
        import torch
    except ImportError as exc:
        raise CollectorError("未安装 whisper 依赖，无法进行转写。") from exc

    # 从环境变量读取 WHISPER_CACHE_DIR 作为 download_root
    if not download_root:
        download_root = os.environ.get("WHISPER_CACHE_DIR") or None

    model_ref = resolve_whisper_model_reference(model_name)
    selected_device = resolve_whisper_device(device_preference, torch)
    log(f"Whisper 转写: model={model_name}, device={selected_device}")
    if model_ref != model_name:
        log(f"使用内置模型文件: {model_ref}")
    if download_root:
        log(f"模型缓存目录: {download_root}")

    try:
        text = run_whisper_transcription(whisper, model_ref, audio_path, language, selected_device, download_root)
        return text, selected_device
    except Exception as exc:
        normalized_preference = normalize_whisper_device(device_preference)
        if selected_device == "cuda" and normalized_preference == "auto":
            log(f"Whisper CUDA 失败，自动回退 CPU: {exc}")
            text = run_whisper_transcription(whisper, model_ref, audio_path, language, "cpu", download_root)
            return text, "cpu"
        if normalized_preference == "cuda":
            raise CollectorError(f"显式要求 CUDA 但执行失败: {exc}") from exc
        raise


def run_whisper_transcription(
    whisper_module: Any, model_ref: str, audio_path: Path, language: str, device: str,
    download_root: str | None = None,
) -> str:
    kwargs: dict[str, Any] = {"fp16": device == "cuda", "verbose": False, "task": "transcribe"}
    if language:
        kwargs["language"] = language
    load_kwargs: dict[str, Any] = {"device": device}
    if download_root:
        load_kwargs["download_root"] = download_root
    with contextlib.redirect_stdout(sys.stderr):
        model = whisper_module.load_model(model_ref, **load_kwargs)
        result = model.transcribe(str(audio_path), **kwargs)

    if isinstance(result, dict):
        segments = result.get("segments") or []
        texts = [str(seg.get("text") or "") for seg in segments if isinstance(seg, dict)]
        text = "\n".join(texts) if texts else str(result.get("text") or "")
        return text
    raise CollectorError("Whisper 返回了无法识别的结果结构。")


def resolve_whisper_device(device_preference: str, torch_module: Any) -> str:
    normalized = normalize_whisper_device(device_preference)
    if normalized == "cpu":
        log("Whisper 设备已显式指定为 CPU")
        return "cpu"

    cuda_available = bool(torch_module.cuda.is_available())
    if normalized == "cuda":
        if cuda_available:
            return "cuda"
        raise CollectorError("显式要求 CUDA 但 torch.cuda 不可用")

    nvidia_detected = detect_nvidia_gpu_simple()
    if cuda_available:
        log("torch.cuda 可用，使用 GPU 转写")
        return "cuda"
    if nvidia_detected:
        log("检测到 NVIDIA 显卡但 torch.cuda 不可用，回退 CPU")
    else:
        log("未检测到 GPU，使用 CPU 转写")
    return "cpu"


def detect_nvidia_gpu_simple() -> bool:
    command = shutil.which("nvidia-smi")
    if not command:
        return False
    try:
        completed = subprocess.run(
            [command, "-L"],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0 and bool((completed.stdout or "").strip())


def resolve_whisper_model_reference(model_name: str) -> str:
    bundled_model = PLUGIN_ROOT / "assets" / "whisper" / f"{model_name}.pt"
    if bundled_model.is_file():
        return str(bundled_model)
    return model_name


# ─── 最终化与输出 ─────────────────────────────────────

def format_timestamped_subtitle(output_dir: Path) -> str:
    """读取字幕文件，返回带 [HH:MM:SS] 时间戳的文本。"""
    exts = {".srt", ".vtt", ".json", ".json3", ".srv3", ".ass", ".ssa", ".ttml", ".xml"}
    files = sorted(output_dir.glob("subtitle.*"))
    for fp in files:
        if not fp.is_file() or fp.suffix.lower() not in exts:
            continue
        raw = fp.read_text(encoding="utf-8", errors="ignore")
        suffix = fp.suffix.lower()

        if suffix in {".json", ".json3", ".srv3"}:
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue
            body = data if isinstance(data, list) else (data.get("body") if isinstance(data, dict) else None)
            if not isinstance(body, list):
                continue
            lines = []
            for item in body:
                if not isinstance(item, dict):
                    continue
                from_sec = item.get("from") or item.get("t") or 0
                content = item.get("content") or item.get("text") or ""
                if not content:
                    continue
                sec = int(from_sec)
                hh = sec // 3600
                mm = (sec % 3600) // 60
                ss = sec % 60
                text = re.sub(r"\s+", " ", content).strip()
                lines.append(f"[{hh:02d}:{mm:02d}:{ss:02d}] {text}")
            if lines:
                return "\n".join(lines)

        else:
            # SRT / VTT / ASS / SSA / TTML / XML — 用正则提取时间戳
            lines_raw = raw.split("\n")
            result = []
            i = 0
            while i < len(lines_raw):
                line = lines_raw[i].strip()
                if not line or re.fullmatch(r"\d+", line):
                    i += 1
                    continue
                m = re.match(r"^(\d{2}:\d{2}:\d{2})[\.\,]\d{2,3}\s*-->", line)
                if m:
                    ts = m.group(1)
                    i += 1
                    texts = []
                    while i < len(lines_raw):
                        tl = lines_raw[i].strip()
                        if not tl:
                            break
                        texts.append(tl)
                        i += 1
                    joined = re.sub(r"\s+", " ", " ".join(texts)).strip()
                    if joined:
                        result.append(f"[{ts}] {joined}")
                else:
                    i += 1
            if result:
                return "\n".join(result)
    return ""


def finalize_and_exit(
    text: str,
    metadata_path: Path,
    raw_info_path: Path,
    audio_streams_path: Path,
    transcript_text_path: Path,
    result_path: Path,
    output_dir: Path,
    source: str,
    metadata: dict[str, Any],
    args: argparse.Namespace,
    fallback_log: list[dict[str, Any]],
    transcript_source: str,
    transcript_device: str | None,
    title: str,
    description: str,
    uploader: str,
    duration: int,
) -> None:
    """清洗文本、写入文件、输出 JSON、可选写入 workspace。"""
    cleaned_text = normalize_plain_text(text)
    transcript_text_path.write_text(cleaned_text + ("\n" if cleaned_text else ""), encoding="utf-8")

    result = {
        "ok": True,
        "source": source,
        "outputDir": str(output_dir),
        "title": title,
        "description": description,
        "uploader": uploader,
        "duration": duration,
        "metadataPath": str(metadata_path),
        "rawInfoPath": str(raw_info_path),
        "audioStreamsPath": str(audio_streams_path),
        "audioPath": find_audio_path(output_dir),
        "subtitleFiles": list_subtitle_files(output_dir),
        "transcriptSource": transcript_source,
        "transcriptDevice": transcript_device,
        "transcriptTextPath": str(transcript_text_path),
        "transcriptText": truncate_text(cleaned_text, args.return_text_limit),
        "transcriptTextFull": cleaned_text,
        "transcriptTextTimestamped": format_timestamped_subtitle(output_dir),
        "resultPath": str(result_path),
        "fallbackLog": fallback_log,
    }
    write_json(result_path, result)
    sys.stdout.write(json.dumps(result, ensure_ascii=False))

    # 清理临时目录
    if output_dir.exists():
        shutil.rmtree(output_dir, ignore_errors=True)


def find_audio_path(output_dir: Path) -> str:
    for ext in [".mp3", ".m4a", ".wav", ".aac", ".ogg", ".flac"]:
        p = output_dir / f"audio{ext}"
        if p.exists():
            return str(p)
    return ""


def list_subtitle_files(output_dir: Path) -> list[str]:
    exts = {".srt", ".vtt", ".ass", ".ssa", ".lrc", ".json", ".json3", ".srv3", ".ttml", ".xml"}
    files = []
    for p in output_dir.iterdir():
        if p.is_file() and p.suffix.lower() in exts and p.name.startswith("subtitle"):
            files.append(str(p))
    return files


# ─── 入口 ─────────────────────────────────────────────

if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except CollectorError as exc:
        log(str(exc))
        raise SystemExit(2)
    except Exception as exc:
        log(f"unexpected error: {exc}")
        raise SystemExit(3)
