#!/usr/bin/env python3
import argparse
import os
import re
import sys
import time
from datetime import datetime

import requests


PLATFORM_NAME = "SiliconFlow"
PLATFORM_PREFIX = "siliconflow"
API_URL = "https://api.siliconflow.cn/v1/user/info"
MAX_RETRIES = 3
RETRY_DELAY = 0.5


def split_keys(raw_text):
    return [item.strip() for item in re.split(r"[\s,;/|&]+", raw_text) if item.strip()]


def collect_keys(inputs):
    # 支持“文件路径 + 直接 key”混合输入
    all_keys = []
    for item in inputs:
        if os.path.isfile(item):
            with open(item, "r", encoding="utf-8") as f:
                all_keys.extend(split_keys(f.read()))
        else:
            all_keys.extend(split_keys(item))
    return all_keys


def deduplicate_keep_order(keys):
    seen = set()
    result = []
    for key in keys:
        if key not in seen:
            seen.add(key)
            result.append(key)
    return result


def to_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def fetch_with_retry(session, key):
    # 仅网络异常重试，HTTP 响应交给业务逻辑判断
    headers = {"Authorization": f"Bearer {key}"}
    last_error = ""
    for attempt in range(MAX_RETRIES + 1):
        try:
            response = session.get(API_URL, headers=headers)
            return response, None
        except requests.RequestException as exc:
            last_error = str(exc)
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY)
    return None, last_error


def check_key(session, key):
    response, net_error = fetch_with_retry(session, key)
    if response is None:
        # 网络层失败单独归类为检测失败
        return "fail", f"⚠️检测失败 - 网络错误: {net_error}"

    status_code = response.status_code
    try:
        data = response.json()
    except ValueError:
        data = {}

    user_data = data.get("data") if isinstance(data, dict) else None
    if status_code == 200 and isinstance(user_data, dict):
        grant_balance = to_float(user_data.get("balance"))
        charge_balance = to_float(user_data.get("chargeBalance"))
        total_balance = to_float(user_data.get("totalBalance"))
        if total_balance > 0:
            return "positive", f"✅余额大于0 - 赠送余额: {grant_balance} | 充值余额: {charge_balance} | 总余额: {total_balance}"
        return "zero", f"⭕余额为0 - 赠送余额: {grant_balance} | 充值余额: {charge_balance} | 总余额: {total_balance}"

    message = response.text.strip() or "请求失败"
    return "invalid", f"❌不可用 - 状态: {status_code} | 消息: {message}"


def default_report_name():
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{PLATFORM_PREFIX}-report-{ts}.txt"


def build_report(total_count, lines, positive_keys, zero_keys, fail_keys, invalid_keys, sep):
    now_text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    report_lines = [
        f"{PLATFORM_NAME} API Key 检测报告",
        f"密钥数量: {total_count}",
        f"检测时间: {now_text}",
        "-" * 60,
    ]
    report_lines.extend(lines)
    report_lines.extend(
        [
            f"🟢余额大于0的 API Key ({len(positive_keys)}):",
            sep.join(positive_keys),
            f"🟡余额为0的 API Key ({len(zero_keys)}):",
            sep.join(zero_keys),
            f"🔴不可用的 API Key ({len(invalid_keys)}):",
            sep.join(invalid_keys),
            f"⚠️检测失败的 API Key ({len(fail_keys)}):",
            sep.join(fail_keys),
        ]
    )
    return "\n".join(report_lines)


def parse_args():
    parser = argparse.ArgumentParser(description=f"{PLATFORM_NAME} API Key 批量检测脚本")
    parser.add_argument("inputs", nargs="+", help="输入 key 或 txt 文件路径，可混合")
    parser.add_argument("--report", nargs="?", const="", default=None, help="输出报告到文件，可选自定义文件名")
    parser.add_argument("--sep", default=",", help="最终汇总时 key 的分隔符，默认英文逗号")
    return parser.parse_args()


def main():
    args = parse_args()
    raw_keys = collect_keys(args.inputs)
    keys = deduplicate_keep_order(raw_keys)

    print("▶️开始批量检测 API Key")
    print(f"🔢总输入: {len(raw_keys)} | 去重后: {len(keys)}")
    print("-" * 60)

    positive_keys = []
    zero_keys = []
    fail_keys = []
    invalid_keys = []
    detail_lines = []

    with requests.Session() as session:
        for idx, key in enumerate(keys, start=1):
            line1 = f"[{idx}/{len(keys)}] {key}"
            status, line2 = check_key(session, key)

            print(line1)
            print(line2)
            print("-" * 60)

            detail_lines.append(line1)
            detail_lines.append(line2)
            detail_lines.append("-" * 60)

            if status == "positive":
                positive_keys.append(key)
            elif status == "zero":
                zero_keys.append(key)
            elif status == "fail":
                fail_keys.append(key)
            else:
                invalid_keys.append(key)

    report_file = None
    if args.report is not None:
        # --report 不带文件名时自动生成
        report_file = args.report.strip() if args.report else default_report_name()
        report = build_report(len(keys), detail_lines, positive_keys, zero_keys, fail_keys, invalid_keys, args.sep)
        with open(report_file, "w", encoding="utf-8") as f:
            f.write(report)

    print("⏹️所有 API Key 检测完成")
    if report_file is not None:
        print(f"📄报告已保存: {report_file}")
    print(f"🔢总共: {len(keys)} | ✅余额大于0: {len(positive_keys)} | ⭕余额为0: {len(zero_keys)} | ❌不可用: {len(invalid_keys)} | ⚠️检测失败: {len(fail_keys)}")
    print("-" * 60)
    if len(positive_keys) > 0:
        print("🟢余额大于0的 API Key:")
        print(",".join(positive_keys))
    if len(zero_keys) > 0:
        print("🟡余额为0的 API Key:")
        print(",".join(zero_keys))


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit("\n已中断")
