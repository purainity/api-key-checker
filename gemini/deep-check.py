#!/usr/bin/env python3
import argparse
import json
import os
import re
import signal
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime

import requests


BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT_MODEL = "gemini-2.5-flash-lite"
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
    if status == "可生成":
        display = "✅ 可生成"
    elif status == "到限额":
        display = f"❎ 到限额 ({message})"
    elif status == "零限额":
        display = f"⭕ 零限额 ({message})"
    elif status == "检测失败":
        display = f"⚠️ 检测失败({message})" if message else "⚠️ 检测失败"
    else:
        display = f"❌ 不可用({status})"
    return {
        "key": key,
        "status": status,
        "message": message,
        "last_check": now_str(),
        "display": display,
        "code": code,
    }


def is_quota_zero(payload):
    """判断 429 是否为零限额"""
    if not isinstance(payload, dict):
        return False
    text = str(payload)
    if '"quota_limit_value": "0"' in text:
        return True
    error = payload.get("error") or {}
    if error.get("code") != 429:
        return False
    message = str(error.get("message") or "")
    if re.search(r"limit:\s*0\b", message):
        return True
    details = error.get("details") or []
    for detail in details:
        metadata = detail.get("metadata") or {}
        if str(metadata.get("quota_limit_value", "")).strip() == "0":
            return True
        for violation in detail.get("violations") or []:
            quota_value = violation.get("quotaValue")
            if quota_value is not None and str(quota_value).strip() == "0":
                return True
    return False


def request_check(session, key, retry, model):
    """请求并分类 网络错误和5xx重试 其余按业务分类"""
    last_error = ""
    url = f"{BASE_URL}/models/{model}:generateContent?key={key}"
    payload = {"contents": [{"parts": [{"text": "Hello, world!"}]}]}
    for attempt in range(retry + 1):
        try:
            response = session.post(url, json=payload)
        except requests.RequestException as exc:
            last_error = sanitize_message(exc)
            if attempt < retry:
                time.sleep(min(0.3 * (2**attempt), 2.0))
            continue
        if response.status_code >= 500:
            if attempt < retry:
                time.sleep(min(0.3 * (2**attempt), 2.0))
                continue
        try:
            data = response.json()
        except ValueError:
            data = {"error": {"code": response.status_code, "message": response.text[:500]}}

        # candidates 存在即表示 generateContent 调用成功
        if isinstance(data, dict) and data.get("candidates"):
            return make_report(key, "可生成", "可生成", 200)

        error = data.get("error") if isinstance(data, dict) else None
        code = response.status_code
        message = "未知错误"
        if isinstance(error, dict):
            code = int(error.get("code") or response.status_code or 0)
            message = sanitize_message(error.get("message") or "未知错误")

        if code == 429:
            return make_report(key, "零限额" if is_quota_zero(data) else "到限额", message, 429)
        if code >= 500:
            return make_report(key, "检测失败", message, code)

        status = f"{code} - {message}" if isinstance(code, int) else f"未知 - {message}"
        return make_report(key, status, message, code if isinstance(code, int) else None)

    return make_report(key, "检测失败", f"网络错误 - {last_error}")


def parse_inputs(raw_inputs, cache_data):
    """解析输入 文件文本或无输入读缓存有效key"""
    if not raw_inputs:
        keys = [k for k, v in cache_data.items() if isinstance(v, dict) and str(v.get("status", "")) == "有效密钥"]
        return keys, "cache"

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


def status_sort_key(status):
    """状态排序键"""
    if status == "可生成":
        return (0, 0, status)
    if status == "到限额":
        return (1, 0, status)
    if status == "零限额":
        return (2, 0, status)
    if status == "检测失败":
        return (4, 0, status)
    m = re.match(r"(\d+)\s*-", status)
    return (3, int(m.group(1)), status) if m else (3, 999999, status)


def write_report(mode, total, model, ordered_keys, final_reports):
    """非direct模式写入报告"""
    if mode == "direct":
        return None

    filename = f"deep-report-{datetime.now().strftime('%Y%m%d-%H%M%S')}.txt"
    groups = defaultdict(list)
    for key in ordered_keys:
        groups[final_reports[key]["status"]].append(key)

    lines = [
        "Gemini API 深度检测报告",
        f"密钥数量: {total}",
        f"检测时间: {now_str()}",
        f"测试模型: {model}",
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
    parser = argparse.ArgumentParser(description="批量深度检测 Gemini API Key 可用性")
    parser.add_argument("inputs", nargs="*", help="txt 文件路径或一个/多个 API Key")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="测试模型 默认gemini-2.5-flash-lite")
    parser.add_argument("--retry", type=int, default=3, help="网络错误和 5xx 重试次数，默认 3")
    args = parser.parse_args(argv or sys.argv[1:])

    if args.retry < 0:
        parser.error("--retry 必须 >= 0")

    def on_sigint(signum, frame):
        del signum, frame
        print("\n🔴 收到中断信号 正在安全停止任务")
        raise SystemExit(130)

    signal.signal(signal.SIGINT, on_sigint)

    cache_data = load_cache(CACHE_FILE)
    raw_keys, mode = parse_inputs(args.inputs, cache_data)

    if not raw_keys:
        print("未读取到有效 API Key")
        return 1

    deduped_keys = list(dict.fromkeys(raw_keys))
    total = len(deduped_keys)

    print(f"▶️ 开始批量深度检测 API Key | 测试模型：{args.model}")
    print(f"原始输入 {len(raw_keys)} | 去重后 {total}")
    print("-" * 60)

    progress = {"done": 0}

    def print_progress(result):
        progress["done"] += 1
        print(f"[{progress['done']}/{total}] {result['key']} {result['display']}")

    final_reports = {}

    with requests.Session() as session:
        for key in deduped_keys:
            report = request_check(session, key, args.retry, args.model)
            if report.get("code") == 400 and str(report.get("message", "")) == "User location is not supported for the API use.":
                print("🔴 检测中止：当前 IP 不支持调用 Gemini API")
                return 2
            final_reports[key] = report
            print_progress(report)

    print("-" * 60)

    report_path = write_report(mode, total, args.model, deduped_keys, final_reports)

    if report_path:
        print(f"⏹️ 检测完成！报告已保存: {report_path}")
    else:
        print("⏹️ 检测完成！")

    if mode != "direct":
        counter = Counter(final_reports[k]["status"] for k in deduped_keys)
        print("📊 统计结果:")
        for status in sorted(counter.keys(), key=status_sort_key):
            if status == "可生成":
                print(f"  - 可生成: {counter[status]}")
            elif status == "到限额":
                print(f"  - 到限额: {counter[status]}")
            elif status == "零限额":
                print(f"  - 零限额: {counter[status]}")
            elif status == "检测失败":
                print(f"  - 检测失败: {counter[status]}")
            else:
                print(f"  - 状态: {status} 数量: {counter[status]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
