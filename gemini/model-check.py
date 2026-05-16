#!/usr/bin/env python3
import argparse
import re
import signal
import sys
import time
from datetime import datetime

import requests


BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
MAX_RETRIES = 3
RETRY_DELAY = 0.5
CORE_METHODS = {"generateContent", "embedContent"}


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


def parse_error(resp_json, status_code):
    """提取错误码和错误消息"""
    error = resp_json.get("error") if isinstance(resp_json, dict) else None
    if isinstance(error, dict):
        code = error.get("code", status_code)
        message = error.get("message") or "未知错误"
    else:
        code = status_code
        message = "未知错误"
    return int(code or status_code or 0), sanitize_message(message)


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


def request_check(session, method, url, **kwargs):
    """执行请求并对网络错误和 5xx 自动重试"""
    last_error = ""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = session.request(method, url, **kwargs)
        except requests.RequestException as exc:
            last_error = sanitize_message(exc)
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY)
            continue
        if response.status_code >= 500:
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY)
                continue
            return response, None
        return response, None
    return None, f"网络错误: {last_error}"


def method_payload(model_name, method_name):
    """根据方法名和模型名构造请求体"""
    if method_name == "generateContent":
        # TTS 模型需带 responseModalities=["AUDIO"] 和 speechConfig
        if "tts" in model_name:
            return {
                "contents": [{"role": "user", "parts": [{"text": "Please read: hello"}]}],
                "generationConfig": {
                    "responseModalities": ["AUDIO"],
                    "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": "Kore"}}},
                },
            }
        return {"contents": [{"parts": [{"text": "ping"}]}]}
    if method_name == "countTokens":
        return {"contents": [{"parts": [{"text": "ping"}]}]}
    if method_name == "batchGenerateContent":
        # 返回 400 FAILED_PRECONDITION
        return {
            "batch": {
                "displayName": "test-batch",
                "inputConfig": {
                    "requests": {
                        "requests": [
                            {
                                "request": {"contents": [{"parts": [{"text": "ping"}]}]}
                            }
                        ]
                    }
                },
            }
        }
    if method_name == "embedContent":
        return {"content": {"parts": [{"text": "ping"}]}}
    if method_name == "countTextTokens":
        # 受支持但调用返回 404 NOT_FOUND
        return {"prompt": {"text": "ping"}}
    if method_name == "asyncBatchEmbedContent":
        # 返回 400 FAILED_PRECONDITION
        return {
            "batch": {
                "displayName": "embed-batch-smoke",
                "inputConfig": {
                    "requests": {
                        "requests": [
                            {
                                "request": {
                                    "model": "models/gemini-embedding-001",
                                    "content": {"parts": [{"text": "ping"}]},
                                }
                            }
                        ]
                    }
                },
            }
        }
    if method_name == "predict":
        return {"instances": [{"prompt": "A red paper airplane on a wooden desk"}]}
    if method_name == "predictLongRunning":
        return {"instances": [{"prompt": "A calm ocean wave at sunrise"}]}
    if method_name == "generateAnswer":
        return {
            "contents": [{"parts": [{"text": "What is Gemini API?"}]}],
            "answerStyle": "ABSTRACTIVE",
            "inlinePassages": {
                "passages": [{"id": "p1", "content": {"parts": [{"text": "Gemini API is Google's developer API for generative AI models."}]}}]
            },
        }
    return None


def default_report_name():
    """生成默认报告文件名"""
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"model-report-{ts}.txt"


def build_report(api_key, lines, full_models):
    """构造完整检测报告文本"""
    report_lines = [
        "Gemini API 模型检测报告",
        f"检测时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"测试密钥: {api_key}",
        "-" * 60,
        "",
    ]
    report_lines.extend(lines)
    report_lines.extend([
        "-" * 60,
        "",
        f"完全可用模型({len(full_models)}个):",
    ])
    report_lines.extend(full_models)
    return "\n".join(report_lines)


def main(argv=None):
    """脚本入口 解析参数并执行模型检测"""
    parser = argparse.ArgumentParser(description="Gemini API 模型方法可用性检测")
    parser.add_argument("key", nargs="+", help="输入一个 API Key")
    parser.add_argument("--report", nargs="?", const="", default=None, help="输出报告到文件，可选自定义文件名")
    args = parser.parse_args(argv or sys.argv[1:])

    def on_sigint(signum, frame):
        del signum, frame
        print("\n已中断")
        raise SystemExit(130)

    signal.signal(signal.SIGINT, on_sigint)

    api_key = args.key[0].strip()

    print("开始检测可用模型")
    print("-" * 60)

    lines = []
    full_models = []

    with requests.Session() as session:
        models = []
        page_token = None

        # 分页拉取模型列表 直到没有 nextPageToken
        while True:
            url = f"{BASE_URL}/models?key={api_key}"
            if page_token:
                url += f"&pageToken={page_token}"
            response, network_error = request_check(session, "GET", url)
            if network_error:
                print(f"模型列表获取失败 ({network_error})")
                return 1
            try:
                data = response.json()
            except ValueError:
                data = {}
            code, message = parse_error(data, response.status_code)
            if code == 400 and str(message or "") == "User location is not supported for the API use.":
                print("检测中止：当前 IP 不支持调用 Gemini API")
                return 2
            if response.status_code != 200:
                print(f"模型列表获取失败 ({code} - {message})")
                return 1
            models.extend(data.get("models") or [])
            page_token = data.get("nextPageToken")
            if not page_token:
                break

        simple_models = []
        for model in models:
            name = str(model.get("name") or "")
            if name.startswith("models/"):
                name = name.split("/", 1)[1]
            if name:
                simple_models.append((name, list(model.get("supportedGenerationMethods") or [])))

        total = len(simple_models)
        # 逐个模型按声明方法探测可调用性
        for idx, (model_name, methods) in enumerate(simple_models, start=1):
            if idx > 1:
                print("-" * 60)
                lines.append("-" * 60)
            header = f"[{idx}/{total}] {model_name}"
            print(header)
            lines.append(header)

            has_core_supported = any(m in methods for m in CORE_METHODS)
            has_core_ok = False

            for method_name in methods:
                if method_name == "createCachedContent":
                    # 独立端点且对最小 token 数有要求
                    line = "  ⏩ createCachedContent 独立端点，跳过"
                    print(line)
                    lines.append(line)
                    continue
                if method_name == "bidiGenerateContent":
                    # Live/双向流式能力，不走普通 REST 单次请求链路
                    line = "  ⏩ bidiGenerateContent Live/双向流式方法，跳过"
                    print(line)
                    lines.append(line)
                    continue

                payload = method_payload(model_name, method_name)

                if payload is None:
                    line = f"  ⏩ {method_name} 不支持的测试方法"
                    print(line)
                    lines.append(line)
                    continue

                url = f"{BASE_URL}/models/{model_name}:{method_name}?key={api_key}"
                response, network_error = request_check(session, "POST", url, json=payload)
                if network_error:
                    line = f"  ⚠️ {method_name} 网络错误 - {network_error}"
                    print(line)
                    lines.append(line)
                    continue

                try:
                    data = response.json()
                except ValueError:
                    data = {}

                if response.status_code == 200:
                    line = f"  ✅ {method_name} 可调用"
                    print(line)
                    lines.append(line)
                    if method_name in CORE_METHODS:
                        has_core_ok = True
                    continue

                code, message = parse_error(data, response.status_code)
                if code == 400 and str(message or "") == "User location is not supported for the API use.":
                    print("检测中止：当前 IP 不支持调用 Gemini API")
                    return 2

                # 429 细分为零限额与到限额
                if code == 429:
                    if is_quota_zero(data):
                        line = f"  ⭕ {method_name} 零限额 ({code} - {message})"
                    else:
                        line = f"  ❎ {method_name} 到限额 ({code} - {message})"
                        if method_name in CORE_METHODS:
                            has_core_ok = True
                    print(line)
                    lines.append(line)
                    continue

                # deep-research 模型虽声明支持 generateContent 但仅允许 Interactions API
                if method_name == "generateContent" and code == 400 and "only supports interactions api" in (message or "").lower():
                    line = f"  ⏩ {method_name} 仅支持 Interactions API"
                    print(line)
                    lines.append(line)
                    continue

                line = f"  ❌ {method_name} ({code} - {message})"
                print(line)
                lines.append(line)

            # 核心方法支持且可调用或到限额 视为完全可用模型
            if has_core_supported and has_core_ok:
                full_models.append(model_name)

    print("-" * 60)
    print("检测完成")

    if args.report is not None:
        out = args.report if args.report else default_report_name()
        report = build_report(api_key, lines, full_models)
        with open(out, "w", encoding="utf-8") as f:
            f.write(report)
        print(f"报告已保存: {out}")

    print(f"完全可用模型({len(full_models)}个):")
    for name in full_models:
        print(name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
