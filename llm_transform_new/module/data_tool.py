"""许可证数据处理工具：从 JSON 提取 license 字段、CSV 转 JSON 等。"""

import csv
import hashlib
import json
from pathlib import Path


def _find_json_objects_in_array(text: str) -> list[str]:
    """在文本中找到第一个 JSON 数组内的所有 {...} 对象字符串。"""
    # 找到第一个数组的起始位置 [...]
    arr_match = [m.start() for m in __import__('re').finditer(r'\[', text)]
    obj_strs = []

    for arr_start in arr_match:
        records = []
        i = arr_start + 1
        while i < len(text):
            while i < len(text) and text[i] in ' \t\n\r,':
                i += 1
            if i >= len(text) or text[i] in (']', '}'):
                break
            if text[i] != '{':
                i += 1
                continue
            # 找到匹配的 }
            depth = 1
            j = i + 1
            while j < len(text) and depth > 0:
                if text[j] == '{':
                    depth += 1
                elif text[j] == '}':
                    depth -= 1
                j += 1
            chunk = text[i:j]
            records.append(chunk)
            i = j

        if records:
            obj_strs.extend(records)
            break  # 只处理第一个找到的数组

    return obj_strs


def load_licenses_from_json(file_path: str | Path) -> list[str]:
    """
    从 JSON 文件中读取数据，只提取每条记录中的 "license" 字段。

    支持格式：
    - JSON 数组：  [{"license": "...", ...}, ...]
    - {"RECORDS": [...]} / 嵌套对象
    - JSONL：每行一个 JSON 对象
    - 截断/损坏的 JSON（自动用括号匹配恢复）

    参数：
    - file_path: JSON 文件路径

    返回：
    - list[str]: license 文本字符串列表，可直接 for text in ... 遍历
    """
    file_path = Path(file_path)
    raw = file_path.read_text(encoding="utf-8").strip()

    records = []

    # 格式 1：JSON 数组 [...]
    if raw.startswith("["):
        try:
            records = json.loads(raw)
        except json.JSONDecodeError:
            pass

    # 格式 2：{"RECORDS": [...]} 或单对象
    if not records and raw.startswith("{"):
        try:
            obj = json.loads(raw)
            if isinstance(obj, dict):
                for val in obj.values():
                    if isinstance(val, list):
                        records = val
                        break
                if not records:
                    records = [obj]
        except json.JSONDecodeError:
            # JSON 损坏（如截断），定位数组并逐个提取对象
            for chunk in _find_json_objects_in_array(raw):
                try:
                    records.append(json.loads(chunk))
                except json.JSONDecodeError:
                    pass

    # 格式 3：JSONL，每行一个对象
    if not records:
        for line in raw.splitlines():
            line = line.strip()
            if not line or line in ("{", "}", "[", "]", "{,", "[,"):
                continue
            if line.endswith(","):
                line = line[:-1]
            if line.startswith("{"):
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    pass

    # 只提取 "license" 字段
    return [r["license"] for r in records if "license" in r]


def load_licenses_from_json_as_indexed(file_path: str | Path) -> list[dict]:
    """
    从 JSON 文件中读取数据，提取 "license" 字段，并自动加编号。

    返回格式：
    [
      {"index": 1, "license": "Apache-2.0 and LGPL-2.1+ and MIT\n..."},
      {"index": 2, "license": "..."},
    ]
    """
    licenses = load_licenses_from_json(file_path)
    return [{"index": i + 1, "license": lic} for i, lic in enumerate(licenses)]


def save_licenses_to_json(licenses: list[dict], output_path: str | Path):
    """将提取的 license 数据保存为 JSON 文件。"""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(licenses, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def record_id(record: dict) -> str:
    """生成记录唯一 ID：sha256(purl\\0version\\0license文本) 取前 20 位，用于断点续跑去重。"""
    text = ""
    for f in ("license_text", "license", "原许可证", "原始许可证"):
        val = record.get(f)
        if val is not None and str(val).strip():
            text = str(val).strip()
            break
    key = f"{record.get('purl', '')}\0{record.get('version', '')}\0{text}"
    return hashlib.sha256(key.encode()).hexdigest()[:20]


def load_jsonl(file_path: str | Path) -> list[dict]:
    """读取 JSONL 文件（每行一个 JSON 对象），跳过空行和损坏行；文件不存在返回空列表。"""
    file_path = Path(file_path)
    if not file_path.exists():
        return []
    records = []
    for line in file_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def load_done_ids(file_path: str | Path) -> set[str]:
    """读取 JSONL 里已有的 record_id 集合，用于断点续跑跳过已处理记录。"""
    return {r.get("record_id") for r in load_jsonl(file_path) if r.get("record_id")}


def convert_csv_to_json(input_file: str | Path, output_file: str | Path) -> None:
    """把 CSV 文件转换成 JSON 数组（CSV 的所有字段原样转成 JSON 字段）。

    参数：
    - input_file: CSV 文件路径
    - output_file: 输出的 JSON 文件路径
    """
    input_file = Path(input_file)
    output_file = Path(output_file)
    with open(input_file, encoding="utf-8-sig", newline="") as f:
        data = [dict(row) for row in csv.DictReader(f)]
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"转换 {len(data)} 条 -> {output_file}")


