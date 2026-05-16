#!/usr/bin/env python3
import argparse
import json
import os
import re
import signal
import sys
import threading
import tempfile
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests


API_URL = "https://generativelanguage.googleapis.com/v1beta/models?key={}"
CACHE_FILE = "gemini-cache.json"


def now_str():
    """返回当前时间字符串"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def load_cache(path):
    """加载缓存 异常时返回空字典"""
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_cache_atomic(path, data):
    """原子写入缓存 避免中断损坏"""
    dir_name = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp_path = tempfile.mkstemp(prefix="gemini-cache-", suffix=".tmp", dir=dir_name)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, path)


def sanitize_message(message):
    """脱敏错误消息并折叠空白"""
    text = str(message or "").replace("\n", " ").strip()
    text = re.sub(r"api_key:[^'\s]+", "[密钥隐藏]", text, flags=re.IGNORECASE)
    text = re.sub(r"\bAIzaSy[0-9A-Za-z_\-]{20,}\b", "[密钥隐藏]", text)
    text = re.sub(r"\((?:\d{1,3}(?:\.\d{1,3}){3}|[0-9a-fA-F:]{2,})\)", "[IP隐藏]", text)
    text = re.sub(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", "[IP隐藏]", text)
    text = re.sub(r"\bproject\s+\d+\b|\bproject=\d+\b|\bproject_number:\d+\b|\bprojects/\d+\b", "[项目ID隐藏]", text, flags=re.IGNORECASE)
    text = re.sub(r"https?://\S+", "[链接隐藏]", text)
    return " ".join(text.split())


def split_keys(raw_text):
    """按空白逗号分号拆分key"""
    return [item.strip() for item in re.split(r"[\s,;/|&]+", str(raw_text or "")) if item.strip()]


def make_report(key, status, message="", code=None):
    """统一构造单个key结果"""
    if status == "有效密钥":
        display = "✅ 有效"
    elif status == "检测失败":
        detail = f"({message})" if message else ""
        display = f"⚠️ 检测失败{detail}"
    else:
        display = f"❌ ({status})"
    return {
        "key": key,
        "status": status,
        "message": message,
        "last_check": now_str(),
        "display": display,
        "code": code,
    }


def request_check(session, key, retry, timeout=None):
    """请求并分类 网络错误和5xx重试"""
    last_exc = None
    request_kwargs = {"timeout": timeout} if timeout is not None else {}
    for attempt in range(retry + 1):
        try:
            resp = session.get(API_URL.format(key), **request_kwargs)
            if resp.status_code >= 500:
                if attempt < retry:
                    time.sleep(min(0.3 * (2**attempt), 2.0))
                    continue
            try:
                payload = resp.json()
            except ValueError:
                payload = {"error": {"code": resp.status_code, "message": resp.text[:500]}}

            # models 存在即表示 key 可正常列出模型
            if isinstance(payload, dict) and "models" in payload:
                return make_report(key, "有效密钥", "有效", 200)
            if not isinstance(payload, dict):
                msg = sanitize_message("返回非 JSON 响应")
                return make_report(key, f"未知 - {msg}", msg)

            err = payload.get("error")
            if not isinstance(err, dict):
                msg = sanitize_message("未知错误")
                return make_report(key, f"未知 - {msg}", msg)

            code = err.get("code")
            msg = sanitize_message(err.get("message") or "未知错误")
            if isinstance(code, int) and code >= 500:
                return make_report(key, "检测失败", msg)
            status = f"{code} - {msg}" if isinstance(code, int) else f"未知 - {msg}"
            return make_report(key, status, msg, code if isinstance(code, int) else None)
        except (requests.ConnectionError, requests.Timeout) as exc:
            last_exc = exc
            if attempt < retry:
                time.sleep(min(0.3 * (2**attempt), 2.0))
                continue
            break
        except requests.RequestException as exc:
            last_exc = exc
            break

    return make_report(key, "检测失败", sanitize_message(str(last_exc or "未知网络错误")))


def parse_inputs(raw_inputs, cache_data):
    """支持文件 文本 无输入缓存"""
    if not raw_inputs:
        return list(cache_data.keys()), "cache"

    file_inputs = []
    text_inputs = []
    for item in raw_inputs:
        if os.path.isfile(item):
            file_inputs.append(item)
        else:
            text_inputs.append(item)

    keys = []
    for file_path in file_inputs:
        with open(file_path, "r", encoding="utf-8") as f:
            keys.extend(split_keys(f.read()))
    for item in text_inputs:
        keys.extend(split_keys(item))

    return keys, ("file" if file_inputs else "direct")


def should_skip(key, cache_data, force=False, cache=False):
    """判断是否跳过检测"""
    # force 模式不跳过；cache 模式命中即跳过；默认模式跳过已终结的状态
    if force:
        return False
    record = cache_data.get(key)
    if record is None:
        return False
    if cache:
        return True
    return str(record.get("status", "")) not in ("有效密钥", "检测失败")


def status_sort_key(status):
    """状态排序键"""
    if status == "有效密钥":
        return (0, 0, status)
    if status == "检测失败":
        return (2, 0, status)
    match = re.match(r"(\d+)\s*-", status)
    return (1, int(match.group(1)), status) if match else (1, 999999, status)


def write_report(mode, ordered_keys, final_reports):
    """非direct模式写入报告"""
    if mode == "direct":
        return None

    filename = f"report-{datetime.now().strftime('%Y%m%d-%H%M%S')}.txt"
    groups = defaultdict(list)
    for key in ordered_keys:
        groups[final_reports[key]["status"]].append(key)

    lines = [
        "Gemini API 检测报告",
        f"密钥数量: {len(ordered_keys)}",
        f"检测时间: {now_str()}",
        "-" * 60,
        "",
    ]
    for status in sorted(groups.keys(), key=status_sort_key):
        lines.append(f"[状态: {status}] (数量: {len(groups[status])})")
        lines.extend(groups[status])
        lines.append("")

    with open(filename, "w", encoding="utf-8") as f:
        f.write("\n".join(lines).rstrip() + "\n")
    return filename


def main(argv=None):
    """脚本入口 解析参数并执行批量检测"""
    parser = argparse.ArgumentParser(description="批量检测 Gemini API Key 可用性")
    parser.add_argument("inputs", nargs="*", help="txt 文件路径或一个/多个 API Key")
    parser.add_argument("--threads", type=int, default=10, help="并发线程数，默认 10")
    parser.add_argument("--retry", type=int, default=3, help="网络错误和 5xx 重试次数，默认 3")
    parser.add_argument("--timeout", type=float, default=None, help="请求超时秒数 默认requests超时")
    parser.add_argument("--force", action="store_true", help="无视缓存，全部重测")
    parser.add_argument("--cache", action="store_true", help="乐观模式，命中缓存即跳过")
    args = parser.parse_args(argv or sys.argv[1:])

    if args.force and args.cache:
        parser.error("--force 与 --cache 不能同时使用")
    if args.threads < 1:
        parser.error("--threads 必须 >= 1")
    if args.retry < 0:
        parser.error("--retry 必须 >= 0")
    if args.timeout is not None and args.timeout <= 0:
        parser.error("--timeout 必须 > 0")

    cache_lock = threading.Lock()
    progress_lock = threading.Lock()
    interrupted = threading.Event()
    cache_data = load_cache(CACHE_FILE)

    def on_sigint(signum, frame):
        del signum, frame
        interrupted.set()
        print("\n🔴 收到中断信号 正在安全保存缓存")
        with cache_lock:
            save_cache_atomic(CACHE_FILE, cache_data)
        raise SystemExit(130)

    signal.signal(signal.SIGINT, on_sigint)

    raw_keys, mode = parse_inputs(args.inputs, cache_data)
    if not raw_keys:
        print("未读取到有效 API Key")
        return 1
    deduped_keys = list(dict.fromkeys(raw_keys))

    skip_keys = []
    request_keys = []
    for key in deduped_keys:
        if should_skip(key, cache_data, force=args.force, cache=args.cache):
            skip_keys.append(key)
        else:
            request_keys.append(key)

    print("▶️ 开始批量检测 API Key")
    print(f"原始输入 {len(raw_keys)} | 去重后 {len(deduped_keys)} | 缓存命中跳过 {len(skip_keys)} | 需要请求 {len(request_keys)}")
    print("-" * 60)

    final_reports = {}
    progress = {"done": 0}

    def print_progress(key, display, cached=False):
        with progress_lock:
            progress["done"] += 1
            cache_hit = "⏩命中缓存" if cached else ""
            suffix = f"{cache_hit} {display}" if cache_hit else display
            print(f"[{progress['done']}/{len(deduped_keys)}] {key} {suffix}")

    for key in skip_keys:
        record = cache_data.get(key, {})
        status = str(record.get("status", "未知"))
        report = make_report(key, status, record.get("message", ""), record.get("code"))
        # 保留缓存中的原始检测时间 不覆盖为当前时间
        report["last_check"] = record.get("last_check", "")
        final_reports[key] = report
        print_progress(key, report["display"], cached=True)

    session = requests.Session()

    def worker(key):
        if interrupted.is_set():
            return make_report(key, "检测失败", "任务被中断")
        return request_check(session, key, args.retry, args.timeout)

    executor = ThreadPoolExecutor(max_workers=args.threads)
    try:
        futures = {executor.submit(worker, key): key for key in request_keys}
        for future in as_completed(futures):
            key = futures[future]
            report = future.result()
            final_reports[key] = report
            with cache_lock:
                cache_data[key] = {
                    "status": report["status"],
                    "message": report["message"],
                    "last_check": report["last_check"],
                }
                if report["code"] is not None:
                    cache_data[key]["code"] = report["code"]
                save_cache_atomic(CACHE_FILE, cache_data)
            print_progress(key, report["display"], cached=False)
            if report.get("code") == 400 and str(report.get("message", "")) == "User location is not supported for the API use.":
                interrupted.set()
                print("🔴 检测中止：当前 IP 不支持调用 Gemini API")
                executor.shutdown(wait=False, cancel_futures=True)
                raise SystemExit(2)
    finally:
        executor.shutdown(wait=True)

    print("-" * 60)
    report_path = write_report(mode, deduped_keys, final_reports)
    if report_path:
        print(f"⏹️ 检测完成！报告已保存: {report_path}")
    else:
        print("⏹️ 检测完成！")

    if mode != "direct":
        counter = Counter(final_reports[k]["status"] for k in deduped_keys)
        print("📊 统计结果:")
        for status in sorted(counter.keys(), key=status_sort_key):
            if status == "有效密钥":
                print(f"  - 有效密钥: {counter[status]}")
            elif status == "检测失败":
                print(f"  - 检测失败: {counter[status]}")
            else:
                print(f"  - 状态: {status} 数量: {counter[status]}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
