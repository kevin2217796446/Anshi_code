# -*- coding: utf-8 -*-
import json, shutil, sys
from pathlib import Path
sys.stdout.reconfigure(encoding='utf-8')

SRC = Path('兼容性判定结果/测试结果_去重')
DST = Path('兼容性判定结果/待重跑9.1')
LIST = Path('许可证原文数据中_标为1_6_2的许可证.txt')

target = {t.strip() for t in LIST.read_text(encoding='utf-8').splitlines() if t.strip()}
print('1/6/2 名单:', len(target), '个', flush=True)

DST.mkdir(parents=True, exist_ok=True)

total = cond1 = cond2 = both = 0
for f in SRC.glob('*.json'):
    total += 1
    try:
        d = json.loads(f.read_text(encoding='utf-8'))
    except Exception:
        continue
    orig = d.get('原许可证', '')
    tgt = d.get('目标许可证', '')
    compat = d.get('兼容性')
    is_c1 = (compat == -1)
    is_c2 = (orig in target or tgt in target)
    if is_c1 and is_c2:
        both += 1
    elif is_c1:
        cond1 += 1
    elif is_c2:
        cond2 += 1
    if is_c1 or is_c2:
        shutil.copy2(f, DST / f.name)
    if total % 5000 == 0:
        print(f'进度 {total}，已匹配 {cond1+cond2+both}', flush=True)

matched = cond1 + cond2 + both
print('='*50, flush=True)
print(f'总文件数: {total}', flush=True)
print(f'仅条件1(待定): {cond1}', flush=True)
print(f'仅条件2(含1/6/2): {cond2}', flush=True)
print(f'两者都满足: {both}', flush=True)
print(f'共匹配(并集): {matched}', flush=True)
print(f'输出目录文件数: {len(list(DST.glob("*.json")))}', flush=True)
print(f'输出: {DST}/', flush=True)
