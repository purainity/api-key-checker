# api-key-checker

多个 AI 平台的 API Key 批量检测脚本。支持 OpenRouter / SiliconFlow / DeepSeek 的余额检测、Gemini 的基础检测、深度检测、模型方法可用性检测。

## 功能特点

- 支持直接传入单个或多个 API Key
- 支持从文件读取 API Key 列表（文件内可用空格、换行、`,`、`;`、`/`、`|`、`&` 分隔多个 API Key）
- 支持文件路径与直接 API Key 混合输入
- 自动去重
- 网络错误自动重试
- 复用 TCP session 加速检测

## 环境要求

- Python 3.8+
- 依赖：`requests`

安装依赖：

```bash
pip install requests
```

## 项目结构

```text
api-key-checker/
├── gemini/
│   ├── check-gemini.py            # Gemini API Key 基础检测（List Models 接口）
│   ├── deep-check.py              # Gemini API Key 深度检测（generateContent 实际调用）
│   └── model-check.py             # Gemini API 模型方法可用性检测
├── openrouter-checker.py          # OpenRouter API Key 批量检测脚本
├── siliconflow-checker.py         # SiliconFlow API Key 批量检测脚本
└── deepseek-checker.py            # DeepSeek API Key 批量检测脚本
```

## OpenRouter / SiliconFlow / DeepSeek API Key 批量检测脚本

分别检测 OpenRouter、SiliconFlow 和 DeepSeek 三个平台的 API Key 余额信息，并分类输出。

### 使用方法

```bash
# OpenRouter
python openrouter-checker.py <key或文件> [key或文件...] [--report [文件]] [--sep 分隔符]

# SiliconFlow
python siliconflow-checker.py <key或文件> [key或文件...] [--report [文件]] [--sep 分隔符]

# DeepSeek
python deepseek-checker.py <key或文件> [key或文件...] [--report [文件]] [--sep 分隔符]
```

### 支持参数

- `<key或文件> [key或文件...]`：API Key 或包含 API Key 的文件路径，可混合输入，至少输入一个
- `--report [文件]`：输出报告到文件，不指定文件名时自动生成为 `{平台前缀}-report-YYYYMMDD-HHMMSS.txt`
- `--sep 分隔符`：报告文件汇总列表中 API Key 的分隔符，默认英文逗号 `,`（仅对报告文件有效，为避免弄乱终端，终端输出始终使用英文逗号）

### 使用示例

```bash
# 检测单个 API Key
python openrouter-checker.py key1

# 检测多个 API Key，输出报告
python openrouter-checker.py key1 key2,key3;key4 --report

# 从文件读取 API Key 检测，自定义报告文件名
python siliconflow-checker.py keys.txt --report report.txt

# 混合文件与直接输入，输出报告，自定义分隔符
python deepseek-checker.py keys.txt key1 --report report.txt --sep ";"
```

### 状态分类

| 状态 | 说明 |
| --- | --- |
| ✅ 余额大于0 | API Key 有效且有余额 |
| ⭕ 余额为0 | API Key 有效但余额为0 |
| ❌ 不可用 | API Key 无效（返回 400/403 等错误码） |
| ⚠️ 检测失败 | 网络错误 |

### 报告格式示例

```
OpenRouter API Key 检测报告
密钥数量: 4
检测时间: 2026-05-15 10:00:00
------------------------------------------------------------
[1/4] key1
⭕余额为0 - 总共: 0.0 | 已用: 0.0 | 剩余: 0.0
------------------------------------------------------------
...
🟢余额大于0的 API Key (1):
key1
🟡余额为0的 API Key (2):
key2,key3
🔴不可用的 API Key (1):
key4
```

## Gemini API 检测脚本

### Gemini API Key 基础检测（List Models 接口）

通过列出可用模型（List Models）来判断 API Key 的有效性。

#### 功能特点

- 多线程并发检测
- 网络错误与 5xx 错误自动重试
- 本地缓存 `gemini-cache.json` 已知的 API Key 状态，避免重复测试已知的无效 API Key
- 原子写入缓存，支持 `Ctrl+C` 安全中断并保存缓存
- 默认情况下重测缓存中有效的 API Key，跳过缓存中无效的 API Key，支持缓存跳过、强制重测
- 输入为空时自动重测缓存中有效的 API Key
- 检测到 IP 不支持调用 Gemini API 时自动中止
- 非直接输入模式（从文件读取 API Key）下自动生成报告文件 `report-YYYYMMDD-HHMMSS.txt`

#### 使用方法

```bash
python gemini/check-gemini.py [key或文件...] [--threads 线程数] [--retry 次数] [--timeout 秒数] [--force] [--cache]
```

#### 支持参数

- `[key或文件...]`：API Key 或包含 API Key 的文件路径，可混合输入，不输入时从缓存读取所有有效 API Key 进行检测
- `--threads 线程数`：并发线程数，默认 10
- `--retry 次数`：网络错误和 5xx 重试次数，默认 3
- `--timeout 秒数`：请求超时秒数
- `--force`：忽略缓存，全部重新检测
- `--cache`：乐观模式，命中缓存直接跳过

#### 使用示例

```bash
# 从文件读取 API Key 检测
python gemini/check-gemini.py keys.txt

# 检测多个 API Key，命中缓存直接跳过
python gemini/check-gemini.py key1 key2,key3;key4 --cache

# 指定并发线程数、重试次数与超时秒数
python gemini/check-gemini.py keys.txt --threads 20 --retry 3 --timeout 15

# 读取缓存中所有已知 API Key，并忽略缓存状态强制重新检测
python gemini/check-gemini.py --force
```

#### 状态分类

| 状态 | 说明 |
| --- | --- |
| ✅ 有效 | API Key 有效（能列出模型） |
| ❌ 不可用 | API Key 无效（返回 400/403 等错误码） |
| ⚠️ 检测失败 | 网络错误 |

### Gemini API Key 深度检测（generateContent 实际调用）

通过实际调用 `generateContent` 方法发送请求，验证每个 API Key 是否真的可用。

#### 功能特点

- 支持自定义测试模型
- 对 429 区分零限额与到限额两种状态
- 无输入时支持从缓存中读取“有效密钥”作为默认输入
- 非直接输入模式（从文件读取 API Key）下自动生成报告文件 `deep-report-YYYYMMDD-HHMMSS.txt`

#### 使用方法

```bash
python gemini/deep-check.py [key或文件...] [--model 模型名] [--retry 次数]
```

#### 支持参数

- `[key或文件...]`：API Key 或包含 API Key 的文件路径，可混合输入，不输入时从缓存读取有效 Key 进行检测
- `--model 模型名`：测试模型，默认 `gemini-2.5-flash-lite`
- `--retry 次数`：网络错误和 5xx 重试次数，默认 3

#### 使用示例

```bash
# 从文件读取 API Key 检测
python gemini/deep-check.py keys.txt

# 检测多个 API Key
python gemini/deep-check.py key1,key2

# 指定测试模型与重试次数
python gemini/deep-check.py keys.txt --model gemini-2.5-flash-lite --retry 5
```

#### 状态分类

| 状态 | 说明 |
| --- | --- |
| ✅ 可生成 | 成功返回生成结果 |
| ❎ 到限额 | 429 但 quota 不为 0（当天额度用完，仍视为可用） |
| ⭕ 零限额 | 429 且 quota 为 0（无可用额度） |
| ❌ 不可用 | API 返回 400/403 等错误码 |
| ⚠️ 检测失败 | 网络错误 |

### Gemini API 模型方法可用性检测

针对单个 API Key，遍历其所有可用模型，逐一测试每个模型声明的所有方法（`generateContent`、`embedContent` 等），输出各方法的可调用性。适合排查某个 API Key 在哪些模型上可用。

#### 功能特点

- 自动分页拉取完整模型列表
- 按模型声明的 `supportedGenerationMethods` 方法逐一探测
- 跳过 `createCachedContent`（独立端点）和 `bidiGenerateContent`（双向流式）
- TTS 模型自动使用语音请求体
- 最终汇总完全可用模型（核心方法可调用或到限额的模型）

#### 使用方法

```bash
python gemini/model-check.py <key> [--report [文件]]
```

#### 支持参数

- `<key>`：一个 Gemini API Key
- `--report [文件]`：输出报告到文件，不指定文件名时自动生成为 `model-report-YYYYMMDD-HHMMSS.txt`

#### 使用示例

```bash
# 检测单个 API Key
python gemini/model-check.py key

# 输出报告到自动生成的文件
python gemini/model-check.py key --report

# 输出报告到指定文件
python gemini/model-check.py key --report model-report.txt
```

#### 状态分类

| 状态 | 说明 |
| --- | --- |
| ✅ 可调用 | 该方法调用成功 |
| ❎ 到限额 | 429 且 quota 不为 0（当天额度用完，仍视为可用） |
| ⭕ 零限额 | 429 且 quota 为 0（无可用额度） |
| ❌ 不可用 | API 返回 400/403 等错误码 |
| ⏩ 跳过 | 独立端点（如 `createCachedContent`）、双向流式（如 `bidiGenerateContent`）或仅支持 Interactions API |
| ⚠️ 检测失败 | 网络错误 |

### 退出码说明

- `0`：执行成功
- `1`：一般错误（如未读取到 API Key、请求失败等）
- `2`：当前 IP 不支持调用 Gemini API
- `130`：用户中断（Ctrl+C）

## 注意事项

- 请妥善保管 API Key，不要提交到公开仓库。
- 脚本的输入和输出包含明文 API Key，请注意本地存储安全。
- 不同平台的“余额/额度”字段含义可能随官方接口调整而变化。
- 本项目脚本由 AI 生成，仅用于学习与技术研究，请勿用于非法用途。
