# 非标许可证转换 + 审核系统 文档

**核心逻辑**：

把「非标许可证文本」（如 `The Apache License, Version 2.0`、`GPL-2.0+`）通过「字典映射 + 大模型」标准化成 SPDX / 标准许可证标识（如 `Apache-2.0`、`GPL-2.0-or-later`），再让大模型审核转换结果是否可信，最终输出「通过审核的标准化结果 CSV」和「需人工复核的清单」。

---

## 1. 整体流程

```
                 ┌─────────────────────────────────────────────────┐
                 │  py main.py（自动串起，只需运行一次）              │
                 └─────────────────────────────────────────────────┘
                                   │
    ① map_csv       按知识库字典对输入 CSV 的 license 字段做「整体映射」
        │           ├─ 命中映射 → 直接写 数据/输出/<stem>_标准化结果.csv
        │           ├─ 未命中   → 1. 写 数据/need_check/<stem>_need_check.json（未去重）
        │                        2. 去重生成 数据/去重表/<stem>.json（大模型输入）
        ▼
    ② llm_scan      对「去重表」做 LLM 标准化 → 数据/非标转换数据/<stem>_output.json
        ▼
    ③ verify_result 审核标准化结果 → 数据/非标审核数据/<stem>_verified.json
        ▼
    ④ map_json      用「审核通过」的表把 need_check 回填到输出 CSV，
                    剩余（审核未通过）留在 need_check，
                    并生成 数据/need_check/<stem>_审核未通过去重表.json（人工只看这一份去重表）
                                   │
                     ┌─────────────┴─────────────┐
                     │  人工修改 *_审核未通过去重表.json  │
                     │  （把能定的 correct 改成 true，并修正 license_spdx_name）│
                     └─────────────┬─────────────┘
                                   ▼
    ⑤ fill_need_check.py（手动运行）用改好的去重表，把 need_check 里原本未去重的数据回填到输出 CSV
```

**数量守恒保证**：`输出 CSV 行数 + need_check 行数 == 输入行数`，全流程不丢数据。

---

## 2. 目录结构 / 架构

```
非标转换机器部署/
├── main.py                 # 总入口：编排 ①②③④ 四步
├── fill_need_check.py      # 第 5 步：人工审核后回填
├── requirements.txt        # Python 依赖
├── .env                    # LLM API 配置（provider / base_url / api_key / model）
├── module/                 # 核心模块
│   ├── map_csv.py          # ① 字典映射 + 去重
│   ├── llm_scan.py         # ② 大模型标准化（异步并发 + 断点续跑 + 自动重试）
│   ├── verify_result.py    # ③ 审核标准化结果（自动重试 + 失败跳过）
│   ├── map_json.py         # ④ 用审核通过的表回填 need_check，生成人工复核表
│   ├── call_llm_langchain.py  # 统一 LLM 调用入口（读 .env，支持 deepseek/local/qwen/teco）
│   ├── data_tool.py        # 数据工具（CSV↔JSON、record_id、JSONL 读写）
│   └── LICENSE_NORMALIZER_SKILL.md  # 大模型的标准化规则（llm_scan 的系统提示词来源）
├── tool_calling/           # 大模型可调用的工具
│   ├── split_merge_tools.py  # 表达式拆分/合并（count 验证等内部也走 LLM）
│   ├── search_spdx.py / spdx_tool.py        # SPDX 检索/校验
│   ├── search_scancode.py / scancode_tool.py # ScanCode 检索/校验
│   ├── search_exception.py / exception_tool.py # 异常标识检索/校验
│   ├── search_knowledge.py   # 知识库相似检索（sentence-transformers）
│   └── _embed_model.py       # 向量嵌入模型
└── 数据/                   # 所有数据
    ├── 输入/                # 输入 CSV（放这里，自动选最新）
    ├── 知识库数据/          # 映射字典 & SPDX/ScanCode/异常 知识库
    ├── 输出/                # 最终标准化结果 CSV
    ├── need_check/          # 需人工复核的数据（未映射 / 审核未通过）
    ├── 去重表/              # 去重后的 license_text 表（大模型输入）
    ├── 非标转换数据/        # ② llm_scan 输出（JSONL）
    ├── 非标审核数据/        # ③ verify_result 输出（JSONL）
    └── 日志数据/            # pipeline.log
```

**各模块职责一句话**：
- `map_csv` —— 能查字典的直接映射掉，查不到的丢给大模型。
- `llm_scan` —— 大模型 + 工具链把非标文本转成 SPDX。
- `verify_result` —— 大模型判断转换对不对（correct + confidence）。
- `map_json` / `fill_need_check` —— 把「转换/审核/人工」的结果按 `license_text` 映射回填到输出 CSV。

---

## 3. 数据目录说明

| 目录 | 内容 | 格式 |
|---|---|---|
| `数据/输入/` | 原始输入（自动选最新的文件） | CSV（含 license 字段） |
| `数据/知识库数据/` | 映射字典 + SPDX/ScanCode/异常知识库 | txt / json / csv |
| `数据/输出/` | 最终标准化结果 | CSV：`<stem>_标准化结果.csv` |
| `数据/need_check/` | 需人工复核的数据 | JSON 数组 |
| `数据/去重表/` | 去重后的 license_text 表 | JSON 数组 |
| `数据/非标转换数据/` | llm_scan 转换结果 | JSONL（含 `license_spdx_name`） |
| `数据/非标审核数据/` | verify 审核结果 | JSONL（含 `correct` / `confidence`） |
| `数据/日志数据/` | 运行日志 | `pipeline.log` |

**文件名约定**：所有中间文件都靠同一个 `stem`（输入文件名去掉后缀）关联，例如输入 `xxx.csv` 会生成：
- `数据/输出/xxx_标准化结果.csv`
- `数据/need_check/xxx_need_check.json`
- `数据/need_check/xxx_审核未通过去重表.json`
- `数据/去重表/xxx.json`
- `数据/非标转换数据/xxx_output.json`
- `数据/非标审核数据/xxx_verified.json`

**license 字段自动识别**：输入里许可证字段可以是 `license_text` / `license` / `原许可证` / `原始许可证` 之一，系统自动识别。

---

## 4. 配置说明

### 4.1 `.env` —— LLM 接口配置

```
LLM_DEEPSEEK_BASE_URL / LLM_DEEPSEEK_API_KEY / LLM_DEEPSEEK_MODEL
LLM_QWEN_*            # 阿里云 Qwen
LLM_LOCAL_*           # 本地部署的 Qwen（走内网，支持 Basic 认证）
LLM_TECO_*            # 太初（OpenAI 兼容接口）
```

### 4.2 `main.py` —— 模型 / provider / 并发

```python
SCAN_MODEL      = 'deepseek-V4-flash'   # 转化模型（None = 用 .env 对应 provider 的默认模型）
SCAN_PROVIDER   = "teco"                # 转化 provider：deepseek / local / qwen / teco
SCAN_CONCURRENCY = 15                   # 转化并发数

VERIFY_MODEL      = 'deepseek-V4-flash' # 审核模型
VERIFY_PROVIDER   = "teco"              # 审核 provider
VERIFY_CONCURRENCY = 1                  # 审核并发数
```

> 模型名要与所选 provider 平台一致（大小写敏感）。切换 provider 时，记得同步改 `SCAN_MODEL` / `VERIFY_MODEL`。

### 4.3 映射字典

`数据/知识库数据/` 下的 `dict_correct_new.txt`（主表）和 `dict_correct_new_supplement.txt`（补充表，支持 `#` 注释）是第一步「字典映射」的来源，格式为 `原始license<TAB或双空格>映射结果`，例如：

```
The Apache License, Version 2.0	Apache-2.0
GPL-2.0+	GPL-2.0-or-later
```

---

## 5. 怎么开始运行

### 5.1 环境准备

```bash
# 1) 安装依赖（在项目的 Python 环境里）
pip install -r requirements.txt

# 2) 确认 .env 里 LLM 配置正确（base_url / api_key / model）
```

> 服务器上若连 huggingface 被墙，`main.py` 已内置 `HF_ENDPOINT=https://hf-mirror.com` 镜像。

### 5.2 跑完整流程（人工审核前自动完成）

```bash
# 方式一：自动选 数据/输入/ 下最新的 CSV
py main.py

# 方式二：命令行指定输入 CSV
py main.py /path/to/输入.csv
```

跑完后你会得到：
- `数据/输出/<stem>_标准化结果.csv`（映射命中 + 审核通过的）
- `数据/need_check/<stem>_need_check.json`（审核未通过、原本未去重的数据）
- `数据/need_check/<stem>_审核未通过去重表.json`（**人工只看这一份去重表**）

### 5.3 人工审核 + 回填（第 5 步）

1. 打开 `数据/need_check/<stem>_审核未通过去重表.json`；
2. 把能确定的条目：`correct` 改成 `true`，并把 `license_spdx_name` 改成正确的标准标识；
3. 运行回填：
   ```bash
   py fill_need_check.py
   # 或指定单个去重表文件
   py fill_need_check.py 数据/need_check/<stem>_审核未通过去重表.json
   ```

它会把 `*_need_check.json` 里 `license_text` 命中的行（`correct != false` 或没有 `correct` 字段）回填到输出 CSV 末尾。

---
## 6. 核心模块详细解读

### 6.1 llm_scan.py —— 大模型标准化（转化）

**作用**：把去重表里每个「唯一的非标许可证文本」转成标准 SPDX 标识。

**核心逻辑**（`scan_by_llm`）：

1. **系统提示词**：从 `LICENSE_NORMALIZER_SKILL.md` 里抽取「核心业务规则 / 输出限制 / 标准执行流程」，作为 system prompt 教大模型怎么转。
2. **工具链**：给大模型绑定一组工具（`tool_calling/` 下），让它能查证而不是瞎猜——
   - `split_license_expression` / `merge_license_result`：把复合表达式拆成片段逐个处理再合并；
   - `search_spdx` / `validate_spdx_identifier`：查 / 校验 SPDX 标识；
   - `search_scancode` / `validate_scancode_identifier`：查 / 校验 ScanCode 标识；
   - `search_exception` / `validate_exception_identifier`：查 / 校验异常标识（`WITH xxx`）；
   - `search_similar_licenses`：向量相似检索知识库；
   - `classify_license_type`：判断许可证类型。
3. **多轮对话**：大模型可反复调用工具（最多 50 轮），直到给出最终结果；从最终回复里提取 `license_spdx_name`（支持 JSON / markdown 包裹 / 纯文本兜底）。
4. **并发 + 断点续跑**（`batch_scan`）：异步并发（`Semaphore` 控制并发数），每条按 `record_id` 逐条 append 落盘（JSONL），中断后重跑自动跳过已完成部分。
5. **容错**：对 503/500/429/连接错误自动重试（2s/4s/8s 退避）；重试仍失败则**跳过不落盘**（不把失败误写成 `none`），下次续跑重试。

**输出**：`license_spdx_name`（标准标识；`none` 表示非许可证内容）。

### 6.2 verify_result.py —— 审核

**作用**：判断 llm_scan 的转换结果是否可信。

**核心逻辑**（`verify_one`）：

1. **系统提示词**：`VERIFY_SYSTEM_PROMPT` 写明了转换系统的一整套规则（最优先级规则、WITH 规则、ScanCode 规则、专有许可证、Commercial、未知版本默认映射、组合逻辑 `&`/`|`、GPL 版本规则、异常配对规则等）和评分标准——让审核模型「知道转换是怎么生成的」，再来判断对错。
2. **输入**：`原文: "..."` + `转换结果: "..."`，问大模型这个转换对不对。
3. **输出**：强制返回 JSON `{"correct": true/false, "confidence": "high"/"medium"/"low"}`；`correct` 决定通过与否，`confidence` 表示把握。
4. **并发 + 断点续跑 + 重试 + 失败跳过**：同 llm_scan；审核失败（重试后仍失败）不落盘，避免误写成 `correct=false`。

**输出**：每条记录带上 `correct` + `confidence`，交给 map_json 分流——`correct=true` 回填输出 CSV，`correct=false` 进人工复核。

---

## 7. 单独运行某个步骤

四个步骤也可单独运行（命令行用法见各脚本顶部 docstring）：

```bash
py module/map_csv.py   -i 数据/输入/xxx.csv   # ① 只做字典映射 + 去重
py module/map_json.py  数据/非标审核数据/xxx_verified.json   # ④ 只做回填
```

`llm_scan` / `verify_result` 一般由 `main.py` 调起，不单独手动跑。
