# Maven源多许可证关系判断与映射 文档

对 Maven 组件数据做两件事：**① 判断一个组件主体许可证之间的逻辑关系（AND / OR / 单许可证 / 无明确关系）② 把这些许可证名称映射成标准 SPDX 标识**。最终输出一份「完全映射 + 未映射」合并的完整结果（JSON 和 CSV 两种格式）。

---

## 1. 核心逻辑（总述）

流水线采用「大模型判断关系 → 提取 → 字典映射 → 去重 → 合并」的方式：

1. 单许可证记录不走大模型，直接判定为 `SINGLE`；只有多许可证（`license_text` 含 `<...>` 标签或超长）才调用大模型判断关系，省调用量。
2. 判断出的关系用符号表达：`AND → &`、`OR → |`、无明确关系 → `|`（并保留原 `license_relation` 标记）。
3. 映射时分两层：**完全映射**的记录直接出结果；**含未映射许可证**的记录单独保留原文，最后与完全映射的结果**合并成一个完整文件**，不丢数据。
4. 完整结果再转成 CSV，方便直接用表格工具查看/交接。

---

## 2. 运行流程

```
py main.py（自动串起，一条命令跑完）
    │
    ① classify_license_relations  单/多许可证关系判定（大模型）
    │      输入：Data_input 的 JSON（含 license_text / pom_text）
    │      输出：Data_classify_result/<stem>_classify.jsonl（每行一个对象，断点续跑）
    ▼
    ② extract_multi_license       提取 normalized_license_names，拼成 license 字段
    │      SINGLE→单值；AND→&连接；OR→|连接；无明确关系→|连接并保留 license_relation
    │      输出：Data_extract_result/<stem>_extract.json
    ▼
    ③ map_license_dict            按 dict_correct_new.txt(+supplement) 映射为标准标识
    │      完全映射 → <stem>_extract_mapped.json
    │      含未映射 → <stem>_extract_unmapped_licenses.json（保留原文）
    ▼
    ④ dedupe_license              对完全映射结果做「同一括号级别内」去重
    │      输出：Data_output/<stem>_dedupe.json
    ▼
    ⑤ 合并 + 转 CSV（main.py 内完成）
            complete = dedupe(完全映射) + unmapped_license(未映射原文)
            输出：Data_output/<stem>_complete.json 和 <stem>_complete.csv
```

> 说明：classify 运行期间，main.py 每隔 `--interval` 秒会把「已产出的 classify 结果」跑一遍 ②③④⑤，实现边分类边出结果。

---

## 3. 目录结构

```
多许可证关系判断与映射/
├── main.py                        # 总入口：编排 ①~⑤ 全流程
├── classify_license_relations.py  # ① 大模型判断许可证关系
├── extract_multi_license.py       # ② 提取许可证名称并拼成表达式
├── map_license_dict.py            # ③ 字典映射为标准标识
├── dedupe_license.py              # ④ 括号级别去重
└── 数据/
    ├── Data_input/                # 输入 JSON
    ├── Data_classify_result/      # ① 分类结果（JSONL）
    ├── Data_extract_result/       # ②③ 提取/映射结果
    └── Data_output/               # ④⑤ 最终结果（dedupe / complete JSON / CSV）
```

---

## 4. 数据说明

### 4.1 输入数据（`Data_input/*.json`）

JSON 数组，每条记录字段：

| 字段 | 含义 |
|---|---|
| `purl` | 组件坐标，如 `pkg:maven/...` |
| `version` | 版本 |
| `license_text` | 许可证原始文本（可能含 `<...>` 标签、多个许可证） |
| `license_source` | 许可证来源 |
| `pom_text` | POM 文件内容（供大模型判断组件主体许可证） |
| `source` | 数据来源标记 |

### 4.2 输出数据（`Data_output/*`）

| 文件 | 内容 |
|---|---|
| `<stem>_dedupe.json` | 完全映射 + 去重后的结果 |
| `<stem>_complete.json` | **完整结果 = dedupe + unmapped_license** |
| `<stem>_complete.csv` | 完整结果的 CSV 版 |

每条输出记录字段：`purl`、`version`、`license`（映射后/原文）、`license_relation`（`SINGLE` / `AND` / `OR` / `NO_EXPLICIT_RELATION` / `UNKNOWN`）。

### 4.3 license 关系符号约定

| license_relation | 输出 license 表达式的连接符 |
|---|---|
| `SINGLE` | 单个值 |
| `AND` | `&` 连接 |
| `OR` | `|` 连接 |
| `NO_EXPLICIT_RELATION` / `UNKNOWN` | `|` 连接，并保留原 `license_relation` 标记 |

---

## 5. 配置说明

配置在 `main.py` 顶部的「全局配置」块（命令行参数可覆盖）：

| 配置 | 默认值 | 说明 |
|---|---|---|
| `MODEL` | `deepseek-v4-flash` | 分类用大模型 |
| `API_URL` | `https://api.deepseek.com/chat/completions` | OpenAI 兼容接口 |
| `API_KEY` | 从环境变量 `DEEPSEEK_API_KEY` 读 | 也可 `--api-key` 覆盖 |
| `WORKERS` | 10 | 分类并发数 |
| `MAX_SINGLE_CHARS` | 100 | 超过此长度的 license_text 视为多许可证 |
| `INTERVAL` | 20 | 下游刷新间隔（秒） |
| `DICT_FILE` / `DICT_SUPPLEMENT` | 上级项目 `数据/原始数据/` 下的两个字典 | ③ 映射字典 |

映射字典格式：`原始许可证<TAB或双空格>标准标识`，例如 `The Apache License, Version 2.0<TAB>Apache-2.0`。

---

## 6. 怎么运行

```bash
# 1) 配置 API_KEY（命令行或环境变量）
py main.py                              # 自动选 Data_input 下最新的 JSON
py main.py --input 数据/Data_input/output_1008.json   # 指定输入
py main.py --model deepseek-v4-pro --workers 8        # 覆盖模型/并发
```

跑完后，`数据/Data_output/` 里会生成：
- `<stem>_dedupe.json`
- `<stem>_complete.json`
- `<stem>_complete.csv`

### 单独运行某一步（可选）

```bash
py classify_license_relations.py 输入.json -o 输出.jsonl --model deepseek-v4-flash
py extract_multi_license.py 分类.jsonl -o 提取.json --error-output 错误.json
py map_license_dict.py 提取.json -o 映射.json --dict 字典.txt --dict-supplement 补充.txt
py dedupe_license.py 映射.json -o 去重.json
```

---

## 7. 断点续跑

- `classify_license_relations.py` 逐行写 JSONL，中断后重跑会跳过已分类的记录（按记录内容哈希判重）。

---

## 8. 常见问题

| 现象 | 原因 | 处理 |
|---|---|---|
| 最终 `complete.json` 只有映射结果、没有未映射数据 | `map_license_dict.py` 旧版把未映射也混进 mapped | 用新版（mapped 只放完全映射），重跑 |
| 输出 CSV 里 license 含 `|` / `&` | 那是「或/与」关系的表达，正常 | — |
| `license_relation` 为空 | 大模型 API 报错的记录被跳过 | 查看 `Data_extract_result/*_extract_error.json` |
