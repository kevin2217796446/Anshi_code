#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
整合脚本：补充 maven 源 no_license 字段。

功能链路（针对一个 maven purl + version）：
  1. 爬取 Maven POM，沿 parent 链递归查找许可证     ← 来自 get_maven_original_stru.py
  2. 多许可证关系判断（DeepSeek 大模型判断 AND/OR）  ← 来自 classify_license_relations.py
  3. 提取许可证名并按关系组合（& 或 |）             ← 来自 extract_multi_license.py
  4. 字典映射为标准 SPDX 标识符                     ← 来自 map_license_dict.py
  5. 未映射 → 本地 LLM 兜底标准化 → SPDX 校验        ← 来自 process_data_with_cpu_gitee_llm.py
  6. 输出：标准 SPDX 标识符 / "no_license" / "need_check"

预留接口（供 process_data_with_cpu_gitee_llm.py import 调用）：
    supplement_maven_license(purl, version) -> str

批量入口（独立/测试用）：
    python supplement_maven_no_license.py --input <含 purl,version 的 CSV> --output <输出 CSV>

说明：
  - 所有路径以服务器为准（映射字典 /home/license/auto_renew/dict_correct_new.txt、
    本地 LLM 模块 local_license_llm、DeepSeek API）。
  - 本脚本自包含：爬取 + 关系判断 + 映射 + LLM 兜底代码均内联，不 import 其他业务脚本。
"""

import os
import re
import time
import random
import hashlib
import json
import traceback
import threading
import argparse
import base64
import ssl
import urllib.request
import urllib.error

import pandas as pd
import requests

from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock

# 本地 LLM（服务器端模块，与 process_data_with_cpu_gitee_llm.py 的用法一致）
try:
    from local_license_llm import normalize_unmapped_license_with_local_llm
except ImportError:  # 本地无此模块时置空，运行时再报错
    normalize_unmapped_license_with_local_llm = None


# =========================================================
# 全局配置
# =========================================================

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# ── 映射字典（服务器路径，与 process_data_with_cpu_gitee_llm.py 一致）──
DICT_FILE = "/home/license/auto_renew/dict_correct_new.txt"

# ── 关系判断模型后端（来自 classify_license_relations.py）──
# 后端可选：'deepseek'（远程 API）或 'local'（本地 OpenAI 兼容模型）
# 可通过环境变量 RELATION_MODEL_BACKEND 切换，或调用接口时传 relation_backend 参数
RELATION_MODEL_BACKEND = os.environ.get("RELATION_MODEL_BACKEND", "local")

# DeepSeek 配置
DEEPSEEK_API_URL = "https://api.deepseek.com/chat/completions"
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_MODEL = "deepseek-v4-flash"

# 本地模型配置（OpenAI 兼容 /chat/completions 接口，如 vLLM / Ollama / 本地推理服务）
# 与 process_data_with_cpu_gitee_llm.py（config.yaml）保持一致
LOCAL_MODEL_API_URL = os.environ.get("LOCAL_MODEL_API_URL", "https://111.0.18.160:8443/api/qwen36/v1/chat/completions")
LOCAL_MODEL_API_KEY = os.environ.get("LOCAL_MODEL_API_KEY", "")
LOCAL_MODEL_NAME = os.environ.get("LOCAL_MODEL_NAME", "qwen3.6-27b")
# 本地模型 Basic 认证（部分自建推理服务用 user/password 而非 Bearer token）
LOCAL_MODEL_USER = os.environ.get("LOCAL_MODEL_USER", "sectrend")
LOCAL_MODEL_PASSWORD = os.environ.get("LOCAL_MODEL_PASSWORD", "Sectrend1234%^&*")

# watch 循环最大运行时长（秒）：防止 process_data 的 PID 被复用导致 supplement 永不退出；
# 默认 24 小时，可通过环境变量 SUPPLEMENT_MAX_RUNTIME 覆盖
MAX_RUNTIME = float(os.environ.get("SUPPLEMENT_MAX_RUNTIME", "86400"))

# watch 模式下，process_data 退出后"最终补充"的并发数（process_data 已结束，可调大加速收尾）
WORKERS_FINAL = int(os.environ.get("SUPPLEMENT_WORKERS_FINAL", "10"))

MAX_LICENSE_CHARS = 8000    # 送入模型的 license_text 最大字符数
MAX_POM_CHARS = 12000       # 送入模型的 POM 上下文最大字符数
MAX_SINGLE_CHARS = 100      # 单许可证 license_text 最大长度（超过视为多许可证）
MAX_TOKENS = 4096
RETRIES = 3
TIMEOUT = 120

# ── 爬取（来自 get_maven_original_stru.py）──
CACHE_DIR = os.path.join(SCRIPT_DIR, "pom_cache")
MAX_PARENT_DEPTH = 30
REQUEST_TIMEOUT = 5
SLEEP_MIN = 0.001
SLEEP_MAX = 0.005
MAX_WORKERS = 50
VERBOSE = False            # 接口调用场景默认安静；批量入口可用 --verbose 打开
PROXY_URL = ""             # 服务器直连外网
RETRY_TOTAL = 1
RETRY_BACKOFF = 0.5


# =========================================================
# 第一块：POM 爬取与许可证提取（来自 get_maven_original_stru.py）
# =========================================================

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 "
        "(Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 "
        "(KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,"
        "application/xml;q=0.9,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive"
}

M2_LOCAL = os.path.join(os.path.expanduser("~"), ".m2", "repository")

REPOSITORIES = [
    ("M2_LOCAL",              M2_LOCAL,                                                         True),
    ("Maven_Central",         "https://repo1.maven.org/maven2",                                 False),
    ("Maven_Central_Alt",     "https://repo.maven.apache.org/maven2",                           False),
    ("Aliyun",                "https://maven.aliyun.com/repository/public",                     False),
    ("Huawei",                "https://mirrors.huaweicloud.com/repository/maven",               False),
    ("Tencent",               "https://mirrors.cloud.tencent.com/repository/maven",             False),
    ("Tsinghua",              "https://mirrors.tuna.tsinghua.edu.cn/maven2",                    False),
    ("Atlassian_External",    "https://packages.atlassian.com/mvn/maven-atlassian-external",    False),
    ("Atlassian",             "https://maven.artifacts.atlassian.com",                          False),
    ("Atlassian_3rdParty",    "https://packages.atlassian.com/maven-3rdparty",                 False),
    ("Atlassian_Public",      "https://packages.atlassian.com/maven-public",                   False),
    ("Atlassian_New",         "https://maven.atlassian.com/content/repositories/atlassian-public", False),
    ("Spring",                "https://repo.spring.io/release",                                 False),
    ("JitPack",               "https://jitpack.io",                                             False),
    ("Aliyun_Google",         "https://maven.aliyun.com/repository/google",                     False),
    ("Google",                "https://dl.google.com/dl/android/maven2",                        False),
    ("Gradle_Plugins",        "https://plugins.gradle.org/m2",                                  False),
    ("Sonatype_Releases",     "https://oss.sonatype.org/content/repositories/releases",          False),
    ("RedHat",                "https://maven.repository.redhat.com/ga",                         False),
    ("JBoss",                 "https://repository.jboss.org/nexus/content/repositories/releases", False),
    ("JBoss_EA",              "https://repository.jboss.org/nexus/content/repositories/ea",      False),
    ("Clojars",               "https://repo.clojars.org",                                      False),
    ("OW2",                   "https://repository.ow2.org/nexus/content/repositories/releases",  False),
    ("Confluent",             "https://packages.confluent.io/maven",                            False),
    ("WSO2",                  "https://maven.wso2.org/nexus/content/repositories/releases",      False),
    ("WSO2_Public",           "https://maven.wso2.org/nexus/content/repositories/public",       False),
    ("Mulesoft",              "https://repository.mulesoft.org/nexus/content/repositories/public", False),
    ("KtorEAP",               "https://maven.pkg.jetbrains.space/public/p/ktor/eap",           False),
    ("Gigaspaces",            "https://maven-repository.openspaces.org",                       False),
    ("BeDataDriven",          "https://nexus.bedatadriven.com/content/groups/public",          False),
    ("Alfresco",              "https://artifacts.alfresco.com/nexus/content/repositories/public", False),
    ("Cloudera",              "https://repository.cloudera.com/content/repositories/public",     False),
    ("XWiki",                 "https://maven.xwiki.org/releases",                               False),
    ("Unvus",                 "https://repo.unvus.com/repository/maven-public",                False),
    ("Fit2Cloud",             "https://repository.fit2cloud.com/repository/fit2cloud-public",  False),
    ("Sonatype_Snapshots",    "https://oss.sonatype.org/content/repositories/snapshots",         False),
    ("Apache_Snapshots",      "https://repository.apache.org/snapshots",                        False),
]

_thread_local = threading.local()
_cache_lock = Lock()
_print_lock = Lock()
_write_lock = Lock()


def _log(*args, **kwargs):
    if not VERBOSE:
        return
    with _print_lock:
        print(*args, **kwargs)


def log_step(message):
    """带时间戳的日志，不受 VERBOSE 影响，用于关键进度信息。"""
    with _print_lock:
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())}] [INFO] {message}")


def get_session():
    if not hasattr(_thread_local, "session"):
        _thread_local.session = requests.Session()
        _thread_local.session.trust_env = False
        from requests.adapters import HTTPAdapter
        from urllib3.util.retry import Retry
        retry_strategy = Retry(
            total=RETRY_TOTAL,
            backoff_factor=RETRY_BACKOFF,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET"],
        )
        adapter = HTTPAdapter(max_retries=retry_strategy, pool_connections=20, pool_maxsize=20)
        _thread_local.session.mount("http://", adapter)
        _thread_local.session.mount("https://", adapter)
        _thread_local.session.headers.update(HEADERS)
        if PROXY_URL:
            _thread_local.session.proxies = {"http": PROXY_URL, "https": PROXY_URL}
    return _thread_local.session


MEMORY_CACHE = {}


def clean_text(text):
    return re.sub(r"\s+", " ", str(text)).strip()


def safe_filename(text):
    return hashlib.md5(text.encode()).hexdigest() + ".xml"


def ensure_cache_dir():
    os.makedirs(CACHE_DIR, exist_ok=True)


def random_sleep():
    time.sleep(random.uniform(SLEEP_MIN, SLEEP_MAX))


def parse_maven_purl(purl):
    """解析 pkg:maven/group/artifact -> (group_id, artifact_id)"""
    try:
        if pd.isna(purl):
            return None, None
        purl = str(purl).strip()
        if not purl.startswith("pkg:maven/"):
            return None, None
        body = purl.replace("pkg:maven/", "", 1)
        body = body.split("@")[0].split("?")[0].split("#")[0]
        parts = body.split("/")
        if len(parts) < 2:
            return None, None
        return ".".join(parts[:-1]), parts[-1]
    except Exception:
        return None, None


def build_pom_path(group_id, artifact_id, version):
    group_path = group_id.replace(".", "/")
    return f"{group_path}/{artifact_id}/{version}/{artifact_id}-{version}.pom"


def is_valid_pom_xml(text):
    if not text:
        return False
    stripped = text.strip()
    return "<project" in stripped[:5000]


_GENERAL_REPOS = {
    "M2_LOCAL", "Aliyun", "Huawei", "Tencent", "Tsinghua",
    "Maven_Central", "Maven_Central_Alt",
}

_REPO_PREFIX_MAP = {
    "Atlassian_External":  [],
    "Atlassian":           ["com.atlassian."],
    "Atlassian_3rdParty":  [],
    "Atlassian_Public":    [],
    "Atlassian_New":       [],
    "Spring":              ["org.springframework.", "org.springdoc."],
    "JBoss":               ["org.jboss.", "org.hibernate.", "org.jbpm.", "org.drools."],
    "JBoss_EA":            ["org.jboss.", "org.hibernate."],
    "WSO2":                ["org.wso2."],
    "WSO2_Public":         [],
    "Mulesoft":            [],
    "Hortonworks":         ["org.apache.hadoop.", "com.hortonworks."],
    "Cloudera":            [],
    "Confluent":           ["io.confluent.", "org.apache.kafka."],
    "Google":              ["com.google.", "com.android.", "androidx."],
    "Aliyun_Google":       ["com.google.", "com.android.", "androidx."],
    "XWiki":               ["org.xwiki."],
    "Gigaspaces":          ["org.openspaces.", "com.gigaspaces."],
    "KtorEAP":             ["io.ktor."],
    "Alfresco":            [],
    "BeDataDriven":        [],
    "Gradle_Plugins":      [],
    "RedHat":              [],
    "Clojars":             [],
    "OW2":                 [],
    "Sonatype_Releases":   [],
    "Sonatype_Snapshots":  [],
    "Apache_Snapshots":    [],
    "JitPack":             [],
}


def _should_try_repo(repo_name, group_id):
    if repo_name in _GENERAL_REPOS:
        return True
    prefixes = _REPO_PREFIX_MAP.get(repo_name)
    if prefixes is None:
        return True
    if not prefixes:
        return True
    return any(group_id.startswith(p) for p in prefixes)


_rate_limit_hits = 0
_rate_limit_lock = Lock()


def _try_one_repo(repo_name, repo_base, is_local, pom_rel, cache_key, cache_path, url_path, try_version):
    if is_local:
        local_path = os.path.join(repo_base, pom_rel)
        if os.path.exists(local_path):
            try:
                with open(local_path, "r", encoding="utf-8") as f:
                    xml_text = f.read()
                if is_valid_pom_xml(xml_text):
                    source_url = "file:///" + local_path.replace("\\", "/")
                    with _cache_lock:
                        with open(cache_path, "w", encoding="utf-8") as cf:
                            cf.write(xml_text)
                        with open(url_path, "w", encoding="utf-8") as uf:
                            uf.write(source_url)
                        MEMORY_CACHE[cache_key] = (xml_text, repo_name, try_version, source_url)
                    return MEMORY_CACHE[cache_key]
            except Exception:
                pass
        return None

    url = f"{repo_base}/{pom_rel}"
    try:
        session = get_session()
        resp = session.get(url, timeout=REQUEST_TIMEOUT)
        if resp.status_code != 200:
            if resp.status_code == 429:
                global _rate_limit_hits
                with _rate_limit_lock:
                    _rate_limit_hits += 1
                    n = _rate_limit_hits
                if n % 10 == 0:
                    print(f"[429 RATE LIMITED] 累计 {n} 次 | 最近: {repo_name}")
            return None
        xml_text = resp.text
        if not is_valid_pom_xml(xml_text):
            return None
        source_url = url
        with _cache_lock:
            try:
                with open(cache_path, "w", encoding="utf-8") as cf:
                    cf.write(xml_text)
                with open(url_path, "w", encoding="utf-8") as uf:
                    uf.write(source_url)
            except Exception:
                pass
            MEMORY_CACHE[cache_key] = (xml_text, repo_name, try_version, source_url)
        random_sleep()
        return MEMORY_CACHE[cache_key]
    except Exception as e:
        _log(f"[ERROR] {repo_name}: {type(e).__name__}: {e}")
        return None


def fetch_pom(group_id, artifact_id, version, filter_repos=True, preferred_repo=None):
    cache_key = f"{group_id}:{artifact_id}:{version}"
    with _cache_lock:
        if cache_key in MEMORY_CACHE:
            return MEMORY_CACHE[cache_key]

    ensure_cache_dir() #确保目录存在，不存在则创建
    filename = safe_filename(cache_key) #md5算法转化成32位指纹
    cache_path = os.path.join(CACHE_DIR, filename)
    url_path = cache_path + ".url"

    with _cache_lock:
        if os.path.exists(cache_path):
            try:
                with open(cache_path, "r", encoding="utf-8") as f:
                    xml_text = f.read()
                source_url = ""
                if os.path.exists(url_path):
                    with open(url_path, "r", encoding="utf-8") as uf:
                        source_url = uf.read().strip()
                MEMORY_CACHE[cache_key] = (xml_text, "disk_cache", version, source_url)
                return MEMORY_CACHE[cache_key]
            except Exception:
                pass

    versions_to_try = [version]
    is_snapshot = str(version).upper().endswith("-SNAPSHOT")
    if is_snapshot:
        release_ver = str(version)[: -len("-SNAPSHOT")]
        if release_ver:
            versions_to_try.append(release_ver)

    for try_version in versions_to_try:
        pom_rel = build_pom_path(group_id, artifact_id, try_version)
        tried = set()

        if preferred_repo:
            for repo_name, repo_base, is_local in REPOSITORIES:
                if repo_name == preferred_repo:
                    tried.add(repo_name)
                    result = _try_one_repo(repo_name, repo_base, is_local, pom_rel, cache_key, cache_path, url_path, try_version)
                    if result is not None:
                        return result
                    break

        for repo_name, repo_base, is_local in REPOSITORIES:
            if repo_name in tried:
                continue
            if filter_repos and not _should_try_repo(repo_name, group_id):
                continue
            tried.add(repo_name)
            result = _try_one_repo(repo_name, repo_base, is_local, pom_rel, cache_key, cache_path, url_path, try_version)
            if result is not None:
                return result

        if filter_repos:
            for repo_name, repo_base, is_local in REPOSITORIES:
                if repo_name in tried:
                    continue
                result = _try_one_repo(repo_name, repo_base, is_local, pom_rel, cache_key, cache_path, url_path, try_version)
                if result is not None:
                    return result

    with _cache_lock:
        MEMORY_CACHE[cache_key] = (None, None, version, None)
    return MEMORY_CACHE[cache_key]


_HTML_ENTITIES = {
    "&nbsp;":   "&#160;",
    "&copy;":   "&#169;",
    "&reg;":    "&#174;",
    "&trade;":  "&#8482;",
    "&mdash;":  "&#8212;",
    "&ndash;":  "&#8211;",
    "&laquo;":  "&#171;",
    "&raquo;":  "&#187;",
    "&ldquo;":  "&#8220;",
    "&rdquo;":  "&#8221;",
    "&lsquo;":  "&#8216;",
    "&rsquo;":  "&#8217;",
    "&hellip;": "&#8230;",
    "&euro;":   "&#8364;",
    "&pound;":  "&#163;",
    "&yen;":    "&#165;",
    "&sect;":   "&#167;",
    "&deg;":    "&#176;",
    "&plusmn;": "&#177;",
    "&sup2;":   "&#178;",
    "&sup3;":   "&#179;",
    "&micro;":  "&#181;",
    "&middot;": "&#183;",
    "&times;":  "&#215;",
    "&divide;": "&#247;",
    "&alpha;":  "&#945;",
    "&beta;":   "&#946;",
    "&gamma;":  "&#947;",
}


def parse_pom_xml(xml_text):
    try:
        return BeautifulSoup(xml_text, "xml")
    except Exception:
        pass
    fixed = xml_text
    for entity, replacement in _HTML_ENTITIES.items():
        fixed = fixed.replace(entity, replacement)
    if fixed != xml_text:
        try:
            return BeautifulSoup(fixed, "xml")
        except Exception:
            pass
    raise


def extract_licenses(xml_text):
    m = re.search(r'<licenses\b[^>]*>.*?</licenses>', xml_text, re.DOTALL)
    return m.group(0) if m else None


def is_multi_license_name(name):
    if "+" not in name:
        return False
    parts = [p.strip() for p in name.split("+") if p.strip()]
    if len(parts) < 2:
        return False
    for p in parts:
        if not re.search(r"[A-Za-z]", p):
            return False
    return True


def process_license_text(license_text):
    if not license_text or not license_text.strip().startswith("<licenses"):
        return license_text
    soup = BeautifulSoup(license_text, "xml")
    names = []
    for lic in soup.find_all("license"):
        name_tag = lic.find("name")
        if name_tag:
            name = re.sub(r"\s+", " ", name_tag.get_text()).strip()
            if name:
                names.append(name)
    tag_count = len(names)
    has_multi_name = any(is_multi_license_name(n) for n in names)
    if tag_count > 1 or has_multi_name:
        return license_text
    elif tag_count == 1:
        return names[0]
    else:
        return license_text


def extract_parent(soup):
    parent = soup.find("parent")
    if not parent:
        return None
    group_tag = parent.find("groupId")
    artifact_tag = parent.find("artifactId")
    version_tag = parent.find("version")
    if not group_tag or not artifact_tag or not version_tag:
        return None
    return {
        "group_id": clean_text(group_tag.text),
        "artifact_id": clean_text(artifact_tag.text),
        "version": clean_text(version_tag.text),
    }


def resolve_license(group_id, artifact_id, version):
    """爬取 POM 并沿 parent 链递归查找许可证。
    返回 dict: {license, license_source, pom_text, source}
    """
    visited = set()
    current_group, current_artifact, current_version = group_id, artifact_id, version
    depth = 0
    hit_chain = []
    current_pom_text = ""
    current_source_url = ""
    current_repo = None

    while depth < MAX_PARENT_DEPTH:
        current_key = (current_group, current_artifact, current_version)
        if current_key in visited:
            _log(f"[RESULT] depth={depth} | {current_group}:{current_artifact}:{current_version} → cycle_detected")
            return {"license": "cycle_detected", "license_source": "parent_cycle",
                    "pom_text": current_pom_text, "source": current_source_url}
        visited.add(current_key)

        xml_text, repo_name, actual_version, source_url = fetch_pom(
            current_group, current_artifact, current_version, preferred_repo=current_repo
        )

        if xml_text:
            current_pom_text = xml_text
            current_source_url = source_url or ""
            if repo_name and repo_name != "disk_cache":
                current_repo = repo_name

        if not xml_text:
            if depth == 0:
                return {"license": "no_license_404", "license_source": "http_error",
                        "pom_text": current_pom_text, "source": current_source_url}
            else:
                return {"license": "no_license", "license_source": "parent_not_found",
                        "pom_text": current_pom_text, "source": current_source_url}

        hit_chain.append(f"{repo_name}@{actual_version}")

        try:
            soup = parse_pom_xml(xml_text)
        except Exception:
            return {"license": "xml_parse_error", "license_source": "xml_error",
                    "pom_text": current_pom_text, "source": current_source_url}

        licenses = extract_licenses(xml_text)
        if licenses:
            _log(f"[RESULT] depth={depth} | {current_group}:{current_artifact}:{current_version} → pom_license")
            return {"license": licenses, "license_source": "pom_license",
                    "pom_text": current_pom_text, "source": current_source_url}

        parent = extract_parent(soup)
        if not parent:
            return {"license": "no_license", "license_source": "no_parent",
                    "pom_text": current_pom_text, "source": current_source_url}

        current_group = parent["group_id"]
        current_artifact = parent["artifact_id"]
        current_version = parent["version"]
        depth += 1

    return {"license": "no_license", "license_source": "max_depth",
            "pom_text": current_pom_text, "source": current_source_url}


# =========================================================
# 第二块：多许可证关系判断（来自 classify_license_relations.py）
# =========================================================

RELATIONS = {"AND", "OR", "NO_EXPLICIT_RELATION", "SINGLE", "UNKNOWN"}

SYSTEM_PROMPT = """你是开源许可证元数据审计员。你要识别“组件主体许可证”及其逻辑关系，并输出 JSON。

信息源的含义和优先级（必须严格执行）：
1. pom_header_context 是 POM 文件开头。这里的版权/许可证注释、SPDX-License-Identifier 或明确的许可声明代表整个组件，优先级最高。
2. all_declared_licenses 是许可证字段列出的所有许可证，可能同时包含组件主体、示例、第三方内容、内嵌/重打包代码等许可证，不能默认全部视为组件主体许可证。
3. 如果 pom_header_context 明确声明了组件许可证，只以该声明确定 effective_license_names 和关系；all_declared_licenses 只用于名称核对，不得把其中其他许可证加入主体关系。
4. 只有 pom_header_context 没有任何明确许可证声明时，才使用 all_declared_licenses 判断，并将 license_basis 设为 all_declared_licenses_fallback。
5. SPDX-License-Identifier 是高度明确的关系表达式。例如“EPL-2.0 OR GPL-2.0 WITH Classpath-exception-2.0”必须原样理解为 OR；WITH 是许可证例外组合的一部分，不是 AND。

关系判定：
- OR：原文明示任选其一，如 or、either...or、dual licensed、Secondary Licenses、SPDX 中的 OR。
- AND：原文明示多个主体许可证共同适用。不同目录、示例或第三方内容分别采用不同许可证，不应简单判为主体 AND。
- NO_EXPLICIT_RELATION：采用回退许可证字段且存在多个许可证，但没有明确 AND/OR。并列多个 license 节点本身不是关系证据。
- SINGLE：最终组件主体许可证只有一个。
- UNKNOWN：材料异常、冲突或无法判断。

effective_license_names 只列组件主体许可证。excluded_license_names 列出许可证字段中未纳入主体关系的许可证，并简述排除原因。
evidence_quote 必须逐字复制输入中的最短充分原文，不得改写；优先使用 SPDX 行，否则使用明确许可句。没有证据时为空字符串。
confidence 是 0 到 1。证据不明确、来源冲突、UNKNOWN 或 confidence < 0.80 时 needs_human_review=true。
只返回一个 JSON 对象，不要 Markdown。JSON 示例：
{"license_basis":"pom_header","effective_license_names":["EPL-2.0","GPL-2.0 WITH Classpath-exception-2.0"],"normalized_license_names":["EPL-2.0","GPL-2.0 WITH Classpath-exception-2.0"],"excluded_license_names":["MIT"],"exclusion_reason":"MIT 仅适用于内嵌第三方内容","license_relation":"OR","relation_expression":"(EPL-2.0 OR GPL-2.0 WITH Classpath-exception-2.0)","evidence_quote":"SPDX-License-Identifier: EPL-2.0 OR GPL-2.0 WITH Classpath-exception-2.0","evidence_location":"pom_header","reasoning_summary":"POM 开头 SPDX 明确声明组件主体为二选一许可","confidence":0.99,"needs_human_review":false,"human_review_reasons":[]}"""


def extract_license_names(text):
    names = re.findall(r"<license\b[^>]*>.*?<name\b[^>]*>(.*?)</name>", text, re.I | re.S)
    return [re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", x)).strip() for x in names]


def compact_pom(pom, max_chars):
    return pom[:max_chars], len(pom) > max_chars


def compact_text(text, max_chars):
    return text[:max_chars], len(text) > max_chars


def record_id(item):
    identity = f"{item.get('purl', '')}\0{item.get('version', '')}\0{item.get('source', '')}"
    return hashlib.sha256(identity.encode()).hexdigest()[:20]


def is_multi_license(license_text, max_single_chars=MAX_SINGLE_CHARS):
    text = str(license_text or "").strip()
    if not text:
        return False
    if "<" in text or ">" in text:
        return True
    return len(text) > max_single_chars


def _relation_model_config(backend=None):
    """返回关系判断模型的 (api_url, api_key, model_name, user, password)。backend 为空时用全局配置。"""
    backend = backend or RELATION_MODEL_BACKEND
    if backend == "local":
        return LOCAL_MODEL_API_URL, LOCAL_MODEL_API_KEY, LOCAL_MODEL_NAME, LOCAL_MODEL_USER, LOCAL_MODEL_PASSWORD
    return DEEPSEEK_API_URL, DEEPSEEK_API_KEY, DEEPSEEK_MODEL, "", ""


def single_license_result(item):
    rid = record_id(item)
    text = str(item.get("license_text", "") or "").strip()
    names = extract_license_names(text) or ([text] if text else [])
    return {
        "record_id": rid,
        "purl": item.get("purl"),
        "version": item.get("version"),
        "license_relation": "SINGLE",
        "normalized_license_names": names,
        "effective_license_names": names,
        "processing_status": "success",
        "model": _relation_model_config()[2],
    }


def call_model(api_url, api_key, model, payload, timeout, max_tokens, user="", password=""):
    body = json.dumps({
        "model": model,
        "temperature": 0,
        "max_tokens": max_tokens,
        "thinking": {"type": "disabled"},
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
    }, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if user and password:
        auth = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
        headers["Authorization"] = f"Basic {auth}"
    else:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(api_url, data=body, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout, context=ssl._create_unverified_context()) as resp:
        response_text = resp.read().decode("utf-8", errors="replace").strip()
    if not response_text:
        raise ValueError("DeepSeek HTTP 响应体为空")
    try:
        raw = json.loads(response_text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"DeepSeek HTTP 响应不是 JSON: {response_text[:200]!r}") from exc
    try:
        choice = raw["choices"][0]
        content = choice["message"].get("content") or ""
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError(f"DeepSeek 响应结构异常: {response_text[:500]}") from exc
    content = content.strip()
    if not content:
        raise ValueError(f"DeepSeek message.content 为空，finish_reason={choice.get('finish_reason', 'unknown')}")
    if content.startswith("```"):
        content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content, flags=re.I | re.S).strip()
    try:
        result = json.loads(content)
    except json.JSONDecodeError as exc:
        start, end = content.find("{"), content.rfind("}")
        if start < 0 or end <= start:
            raise ValueError(f"模型内容不是有效 JSON: {content[:300]!r}") from exc
        result = json.loads(content[start:end + 1])
    result["_usage"] = raw.get("usage", {})
    return result


def normalize_result(item, result, rid, model, truncated):
    license_text = str(item.get("license_text", ""))
    pom_text = str(item.get("pom_text", ""))
    names = extract_license_names(license_text) or extract_license_names(pom_text)
    basis = str(result.get("license_basis", "unknown"))
    if basis not in {"pom_header", "all_declared_licenses_fallback", "unknown"}:
        basis = "unknown"
    effective_names = result.get("effective_license_names") or result.get("license_names") or names
    normalized_names = result.get("normalized_license_names") or effective_names
    relation = str(result.get("license_relation", "UNKNOWN")).upper()
    if relation not in RELATIONS:
        relation = "UNKNOWN"
    quote = str(result.get("evidence_quote", "")).strip()
    evidence_valid = not quote or quote in license_text or quote in pom_text
    try:
        confidence = max(0.0, min(1.0, float(result.get("confidence", 0))))
    except (TypeError, ValueError):
        confidence = 0.0
    review_reasons = result.get("human_review_reasons", [])
    if not isinstance(review_reasons, list):
        review_reasons = [str(review_reasons)]
    if quote and not evidence_valid:
        review_reasons.append("模型返回的证据不是输入原文的逐字子串")
    if truncated:
        review_reasons.append("送入模型的 POM 许可证相关文本被截断")
    needs_review = bool(result.get("needs_human_review", False)) or confidence < 0.8 or not evidence_valid or relation == "UNKNOWN"
    return {
        "record_id": rid,
        "purl": item.get("purl"),
        "version": item.get("version"),
        "license_basis": basis,
        "effective_license_names": effective_names,
        "normalized_license_names": normalized_names,
        "license_relation": relation,
        "relation_expression": result.get("relation_expression", ""),
        "confidence": confidence,
        "needs_human_review": needs_review,
        "processing_status": "success",
        "model": model,
        "input_truncated": truncated,
        "usage": result.get("_usage", {}),
    }


def classify_one(item, backend=None):
    """对单条记录做多许可证关系判断（DeepSeek 或本地模型）。返回同 normalize_result/single_license_result 结构。"""
    api_url, api_key, model, user, password = _relation_model_config(backend)
    original_license_text = str(item.get("license_text", ""))
    license_text, license_truncated = compact_text(original_license_text, MAX_LICENSE_CHARS)
    relevant_pom, truncated = compact_pom(str(item.get("pom_text", "")), MAX_POM_CHARS)
    payload = {
        "purl": item.get("purl"), "version": item.get("version"),
        "pom_header_context": relevant_pom,
        "all_declared_licenses": license_text,
        "required_output_fields": {
            "license_basis": "pom_header|all_declared_licenses_fallback|unknown",
            "effective_license_names": ["组件主体许可证名称"],
            "normalized_license_names": ["尽可能给出 SPDX 标识；不确定则保留原名"],
            "excluded_license_names": ["许可证字段中未纳入主体关系的名称"],
            "exclusion_reason": "排除原因摘要",
            "license_relation": "AND|OR|NO_EXPLICIT_RELATION|SINGLE|UNKNOWN",
            "relation_expression": "例如 (Apache-2.0 OR LGPL-2.1-or-later)",
            "evidence_quote": "最短充分原文或空字符串",
            "evidence_location": "pom_header|all_declared_licenses|none",
            "reasoning_summary": "简短中文说明", "confidence": "0到1",
            "needs_human_review": "boolean", "human_review_reasons": ["原因"],
        },
    }
    rid = record_id(item)
    error = None
    for attempt in range(RETRIES + 1):
        try:
            result = call_model(api_url, api_key, model, payload, TIMEOUT, MAX_TOKENS, user, password)
            return normalize_result(item, result, rid, model, truncated or license_truncated)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            if attempt < RETRIES:
                time.sleep(min(2 ** attempt, 8))
    return {"record_id": rid, "purl": item.get("purl"), "version": item.get("version"),
            "processing_status": "error", "error": error, "model": model}


# =========================================================
# 第三块：许可证名组合（来自 extract_multi_license.py）
# =========================================================

def combine(names, relation):
    """按 license_relation 组合许可证名称列表。"""
    if relation == "AND":
        return "&".join(names)
    if relation == "OR":
        return "|".join(names)
    if relation == "SINGLE":
        return names[0] if len(names) == 1 else "&".join(names)
    return "|".join(names)


# =========================================================
# 第四块：字典映射 SPDX（来自 map_license_dict.py）
# =========================================================

def load_mapping(path):
    """解析映射文件（tab 分隔，兼容末尾空格分隔）。"""
    if not os.path.exists(path):
        return {}
    mapping = {}
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            if "\t" in line:
                k, v = line.split("\t", 1)
                mapping[k.strip()] = v.strip()
            else:
                parts = re.split(r"\s{2,}", line)
                if len(parts) >= 2:
                    mapping[parts[0].strip()] = parts[-1].strip()
    return mapping


_OP_RE = re.compile(r"\s*([|&])\s*")


def _normalize(text):
    text = re.sub(r"\s+", " ", text).strip()
    return _OP_RE.sub(r"\1", text)


def _is_group_paren(s, open_idx):
    depth = 1
    for i in range(open_idx + 1, len(s)):
        c = s[i]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return False
        elif c in "|&":
            return True
    return False


def _split_tokens(norm):
    tokens = []
    i, n = 0, len(norm)
    while i < n:
        c = norm[i]
        if c in "|&":
            tokens.append(c)
            i += 1
        elif c == "(" and _is_group_paren(norm, i):
            tokens.append("(")
            i += 1
        elif c == ")":
            tokens.append(")")
            i += 1
        else:
            start = i
            depth = 0
            while i < n:
                c = norm[i]
                if c in "|&" and depth == 0:
                    break
                if c == "(":
                    if depth == 0 and _is_group_paren(norm, i):
                        break
                    depth += 1
                elif c == ")":
                    if depth == 0:
                        break
                    depth -= 1
                i += 1
            name = norm[start:i].strip()
            if name:
                tokens.append(name)
    return tokens


def _map_text(text, mapping, pending=None):
    """映射单个 license_text，返回映射后的字符串；未命中 token 记入 pending。"""
    if pending is None:
        pending = set()
    if not text:
        return ""
    norm = _normalize(text)
    if not norm:
        return ""
    if norm in mapping:
        return mapping[norm]
    if not any(ch in norm for ch in "|&"):
        pending.add(norm)
        return norm
    out = []
    for tok in _split_tokens(norm):
        if tok in ("|", "&", "(", ")"):
            out.append(tok)
        elif tok in mapping:
            out.append(mapping[tok])
        else:
            pending.add(tok)
            out.append(tok)
    return "".join(out)


# =========================================================
# 第五块：本地 LLM 兜底 + SPDX 校验（来自 process_data_with_cpu_gitee_llm.py）
# =========================================================

def string_to_md5(s):
    return hashlib.md5(s.encode('utf-8')).hexdigest()


def is_known_spdx_expression(expression, mapping):
    """校验表达式里的每个 token 是否都在已知 SPDX 集合（映射表目标值）中。"""
    known_ids = set()
    for mapped in mapping.values():
        if not mapped or mapped == 'Unknown':
            continue
        known_ids.update(
            token for token in re.split(r'\s*(?:\||&|\(|\)|\bAND\b|\bOR\b|\bWITH\b)\s*', str(mapped)) if token
        )
    ids = [token for token in re.split(r'\s*(?:\||&|\(|\)|\bAND\b|\bOR\b|\bWITH\b)\s*', expression) if token]
    return bool(ids) and all(token in known_ids for token in ids)


# =========================================================
# 第六块：核心整合接口 + 批量入口
# =========================================================

# 映射字典（模块加载时读一次，供接口与批量复用）
MAPPING = load_mapping(DICT_FILE)


def _relation_for_record(relation):
    """把关系映射为要落盘的三个值之一：NO_EXPLICIT_RELATION / SINGLE / UNKNOWN。
    AND / OR 是明确关系（已体现在 license 字符串的 & / | 里），落盘为空字符串。"""
    if relation in ("NO_EXPLICIT_RELATION", "SINGLE", "UNKNOWN"):
        return relation
    return ""


def _supplement_one(purl, version, relation_backend=None):
    """对单个 maven 包补充许可证，返回 (license, original_license, relation)。

    license          标准 SPDX 标识符 / "no_license" / "need_check"
    original_license 当 license == "need_check" 时为未映射的原始许可证文本，否则为空字符串
    relation         许可证关系：NO_EXPLICIT_RELATION / SINGLE / UNKNOWN 之一；
                     AND/OR（明确关系）返回空字符串
    """
    try:
        group_id, artifact_id = parse_maven_purl(purl)
        if not group_id or not artifact_id:
            return "no_license", "", ""

        result = resolve_license(group_id, artifact_id, str(version))
        license_source = result["license_source"]
        pom_text = result["pom_text"]

        if license_source != "pom_license":
            return "no_license", "", ""

        license_text = process_license_text(result["license"])

        # 单/多许可证判定与关系判断
        item = {"purl": purl, "version": version,
                "license_text": license_text, "pom_text": pom_text}
        if not is_multi_license(license_text):
            names = [license_text] if license_text else []
            relation = "SINGLE"
        else:
            cr = classify_one(item, relation_backend)
            if cr.get("processing_status") != "success":
                return "no_license", "", ""
            names = cr.get("normalized_license_names") or cr.get("effective_license_names") or []
            relation = cr.get("license_relation", "UNKNOWN")

        if not names:
            return "no_license", "", ""

        # 记录许可证关系（AND/OR 明确关系已体现在 license 字符串里，落盘为空）
        rel_record = _relation_for_record(relation)

        # 组合 + 字典映射
        combined = combine([str(n) for n in names], relation)
        pending = set()
        mapped = _map_text(combined, MAPPING, pending)
        if not pending:
            return mapped, "", rel_record

        # 本地 LLM 兜底
        if normalize_unmapped_license_with_local_llm is None:
            return "need_check", combined, rel_record
        try:
            llm_result = normalize_unmapped_license_with_local_llm(combined)
        except Exception as exc:
            _log(f"[LLM ERROR] {purl}@{version}: {exc}")
            return "need_check", combined, rel_record
        if llm_result and is_known_spdx_expression(str(llm_result), MAPPING):
            return str(llm_result), "", rel_record
        return "need_check", combined, rel_record

    except Exception as exc:
        _log(f"[ERROR] {purl}@{version}: {type(exc).__name__}: {exc}")
        return "no_license", "", ""


def supplement_maven_license(purl, version, relation_backend=None):
    """预留接口：对 maven no_license 的包补充许可证。

    参数：
        purl             maven 包标识，如 "pkg:maven/group/artifact"
        version          版本号
        relation_backend 关系判断模型后端：'deepseek'（默认）或 'local'；为空则用全局配置

    返回：
        - 标准 SPDX 标识符（如 "MIT"、"Apache-2.0"、"Apache-2.0|GPL-2.0-only"）
        - "no_license"    POM 里确实没有许可证（或爬取/解析失败）
        - "need_check"    有许可证但字典和本地 LLM 都标准化失败，需人工复核
    """
    return _supplement_one(purl, version, relation_backend)[0]


# process_data_with_cpu_gitee_llm.py 的 daily_output CSV 列（无表头）
DAILY_OUTPUT_COLUMNS = ["md5", "source_type", "final_license", "license_source", "purl", "version", "license_relation"]


def _need_check_path(csv_path):
    """根据 daily_output CSV 路径推导 need_check 输出路径（与 process_data 命名一致）。"""
    stem = os.path.basename(csv_path)
    if stem.startswith("license_"):
        stem = stem[len("license_"):]
    if stem.endswith(".csv"):
        stem = stem[:-len(".csv")]
    return os.path.join(SCRIPT_DIR, "need_check", f"license_need_check_{stem}.csv")


def _supplement_result_path(csv_path):
    """根据 daily_output CSV 路径推导 supplement 结果文件路径（放在 tmp_batch 文件夹，保留不删除）。"""
    stem = os.path.basename(csv_path)
    if stem.startswith("license_"):
        stem = stem[len("license_"):]
    if stem.endswith(".csv"):
        stem = stem[:-len(".csv")]
    return os.path.join(SCRIPT_DIR, "tmp_batch", f"supplement_result_{stem}.csv")


def scan_and_supplement(csv_path, output_path=None, relation_backend=None, workers=8, chunksize=20000):
    """扫描 daily_output CSV，找到 maven 源 no_license 记录并补充许可证。

    补充结果【增量追加】写到 supplement_result_<date>.csv（不再原地回填 daily_output），
    最后再调用 merge_supplement_result 把结果合并回 daily_output（覆盖 no_license）。

    幂等：读已有 result 文件里的 md5 集合，跳过已补充过的记录，可被 watch 循环反复安全调用。

    参数：
        csv_path         daily_output/license_<date>.csv 路径（6 列无表头）
        output_path      已废弃（保留签名兼容），结果文件由 csv_path 自动推导
        relation_backend 'deepseek' / 'local'；为空用全局配置
        workers          并发线程数
        chunksize        分块读取的行数（降低内存峰值）

    返回：None
    """
    result_path = _supplement_result_path(csv_path)
    os.makedirs(os.path.dirname(result_path), exist_ok=True)

    # 读已有 result 文件，得到已补充过的 md5（幂等）
    done_md5 = set()
    if os.path.exists(result_path):
        try:
            done_df = pd.read_csv(result_path, header=None, dtype=str)
            done_md5 = set(done_df.iloc[:, 0].tolist())
        except Exception:
            done_md5 = set()

    total_targets = 0
    updated = need_check = still_no = 0
    need_check_records = []  # 收集 need_check 记录，用于单独落盘到 need_check 文件夹

    reader = pd.read_csv(csv_path, header=None, names=DAILY_OUTPUT_COLUMNS, dtype=str, chunksize=chunksize)
    for chunk in reader:
        chunk["md5"] = chunk["md5"].fillna("").astype(str)
        chunk["purl"] = chunk["purl"].fillna("").astype(str)
        chunk["version"] = chunk["version"].fillna("").astype(str)
        chunk["final_license"] = chunk["final_license"].fillna("").astype(str)
        chunk["license_source"] = chunk["license_source"].fillna("").astype(str)

        # 扫描 maven 源 no_license 且尚未补充过的记录
        mask = (chunk["purl"].str.startswith("pkg:maven/")
                & (chunk["final_license"] == "no_license")
                & (chunk["license_source"] == "")
                & (~chunk["md5"].isin(done_md5)))
        targets = chunk.index[mask].tolist()
        total_targets += len(targets)

        if targets:
            chunk_rows = []  # 本 chunk 的补充结果
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = {
                    executor.submit(_supplement_one,
                                    chunk.at[idx, "purl"], chunk.at[idx, "version"], relation_backend): idx
                    for idx in targets
                }
                for future in as_completed(futures):
                    idx = futures[future]
                    lic, original_license, relation = future.result()
                    md5 = chunk.at[idx, "md5"]
                    _log(f"[补充] {chunk.at[idx, 'purl']}@{chunk.at[idx, 'version']} -> {lic}")
                    if lic == "need_check":
                        src = "maven_supplement_need_check"
                        need_check += 1
                        need_check_records.append({
                            "md5_purl_version": md5,
                            "purl": chunk.at[idx, "purl"],
                            "version": chunk.at[idx, "version"],
                            "原始许可证": original_license,
                        })
                    elif lic == "no_license":
                        src = "maven_supplement_no_license"
                        still_no += 1
                    else:
                        src = "maven_supplement"
                        updated += 1
                    chunk_rows.append([md5, chunk.at[idx, "purl"], chunk.at[idx, "version"], lic, src, relation])
                    done_md5.add(md5)

            # 本 chunk 处理完，立即增量追加写入 result 文件（边跑边写，不用等全部爬完）
            if chunk_rows:
                df_chunk = pd.DataFrame(chunk_rows, columns=["md5", "purl", "version", "final_license", "license_source", "license_relation"])
                df_chunk.to_csv(result_path, index=False, header=False, mode="a", encoding="utf-8")

    # 清理内存缓存，释放内存（磁盘缓存 pom_cache 已持久化，不影响正确性，仅下次改从磁盘读）
    with _cache_lock:
        MEMORY_CACHE.clear()

    # 把 need_check 单独写到 need_check 文件夹（与 process_data 的落盘方式对齐）
    if need_check_records:
        need_check_path = _need_check_path(csv_path)
        os.makedirs(os.path.dirname(need_check_path), exist_ok=True)
        ndf = pd.DataFrame(need_check_records, columns=["md5_purl_version", "purl", "version", "原始许可证"])
        ndf = ndf.drop_duplicates()
        ndf.to_csv(need_check_path, index=False, encoding="utf-8-sig")
        log_step(f"need_check 已写入: {need_check_path}（{len(ndf)} 条）")

    log_step(f"补充完成：待补充 {total_targets} 条，成功 {updated} 条，need_check {need_check} 条，仍 no_license {still_no} 条 -> {result_path}")
    return None


def merge_supplement_result(csv_path):
    """把 supplement 结果文件合并回 daily_output，覆盖其中的 no_license 记录。

    结果文件列：md5, purl, version, final_license, license_source（无表头）。
    """
    result_path = _supplement_result_path(csv_path)
    if not os.path.exists(result_path):
        log_step("[合并] 无 supplement 结果文件，跳过合并")
        return
    result_df = pd.read_csv(result_path, header=None,
                            names=["md5", "purl", "version", "final_license", "license_source", "license_relation"], dtype=str)
    if result_df.empty:
        return
    result_df = result_df.drop_duplicates(subset=["md5"], keep="last")
    result_map = dict(zip(result_df["md5"], zip(result_df["final_license"], result_df["license_source"], result_df["license_relation"])))
    result_keys = set(result_map.keys())

    daily_df = pd.read_csv(csv_path, header=None, names=DAILY_OUTPUT_COLUMNS, dtype=str)
    daily_df["md5"] = daily_df["md5"].fillna("").astype(str)
    daily_df["final_license"] = daily_df["final_license"].fillna("").astype(str)

    mask = (daily_df["final_license"] == "no_license") & daily_df["md5"].isin(result_keys)
    daily_df.loc[mask, "final_license"] = daily_df.loc[mask, "md5"].map(lambda m: result_map[m][0])
    daily_df.loc[mask, "license_source"] = daily_df.loc[mask, "md5"].map(lambda m: result_map[m][1])
    daily_df.loc[mask, "license_relation"] = daily_df.loc[mask, "md5"].map(lambda m: result_map[m][2])

    daily_df[DAILY_OUTPUT_COLUMNS].to_csv(csv_path, index=False, header=False, encoding="utf-8")
    log_step(f"[合并] supplement 结果已合并回 {os.path.basename(csv_path)}，覆盖 no_license {int(mask.sum())} 条")


def clear_supplement_result(csv_path):
    """清空（删除）指定日期的 supplement 结果文件，用于启动时清理旧结果，避免同一天重跑时残留。"""
    result_path = _supplement_result_path(csv_path)
    if os.path.exists(result_path):
        os.remove(result_path)
        log_step(f"[清理] 已删除旧 supplement 结果文件: {result_path}")


def _pid_alive(pid):
    """判断进程是否存活。

    Windows 上用 OpenProcess 探测进程句柄（绝不发信号）；
    os.kill(pid, 0) 在 Windows 上 sig=0 等于 CTRL_C_EVENT，可能误发 Ctrl+C，故只在 Unix 上使用。
    """
    if os.name == 'nt':
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if handle:
            kernel32.CloseHandle(handle)
            return True
        # 打开失败：ERROR_INVALID_PARAMETER(87) 表示 PID 不存在；其余（如 ACCESS_DENIED=5）视为存活
        return kernel32.GetLastError() != 87
    else:
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False


def watch_and_supplement(csv_path, parent_pid=None, output_path=None,
                         relation_backend=None, workers=2, workers_final=None, interval=30, max_runtime=MAX_RUNTIME):
    """watch 循环：反复扫描 CSV 回填 maven no_license，直到 process_data 退出或超时后补最后一轮退出。

    并发策略：
      - process_data 运行期间（并行）：用 workers（小并发，默认 2），避免和 process_data 抢资源
      - process_data 退出后（最终补充）：用 workers_final（大并发，默认 10），快速收尾

    配合 process_data_with_cpu_gitee_llm.py 使用：
      - process_data 启动时用 subprocess.Popen 后台启动本脚本（--watch --parent-pid <pid>）
      - 本脚本检测到 process_data 进程退出 → 最后一轮补充 → 退出（不生成 done 标记文件）
      - 超过 max_runtime 秒仍未退出 → 强制退出（防止 PID 复用导致永不退出）

    参数：
        csv_path         daily_output CSV 路径
        parent_pid       process_data 的进程 PID；为空则永不退出（需外部 kill）
        output_path      输出路径；为空则原地写回 csv_path
        relation_backend 'deepseek' / 'local'
        workers          process_data 运行期间的并发线程数（默认 2，调小避免抢资源）
        workers_final    process_data 退出后的最终补充并发数（默认 10，调大加速收尾）
        interval         每轮扫描间隔（秒）
        max_runtime      最大运行时长（秒），超时强制退出
    """
    if workers_final is None:
        workers_final = WORKERS_FINAL
    start_time = time.time()
    log_step(f"[WATCH] 启动：csv={csv_path} parent_pid={parent_pid} workers={workers} workers_final={workers_final} interval={interval}s max_runtime={max_runtime}")
    # 启动时清空旧的 supplement 结果文件，避免同一天重跑时残留旧结果
    clear_supplement_result(csv_path)
    while True:
        # 本轮：扫描 + 回填（CSV 存在才处理；process_data 运行期间用小并发）
        if os.path.exists(csv_path):
            try:
                scan_and_supplement(csv_path, output_path, relation_backend, workers)
            except Exception as exc:
                log_step(f"[WATCH] 本轮补充失败: {type(exc).__name__}: {exc}")

        # 退出条件 1：process_data 已退出
        parent_exited = bool(parent_pid) and not _pid_alive(parent_pid)
        # 退出条件 2：超过最大运行时长（防止 PID 复用导致永不退出）
        timed_out = bool(max_runtime) and (time.time() - start_time > max_runtime)

        if parent_exited or timed_out:
            # 最后一轮：process_data 已退出，用大并发快速收尾
            if os.path.exists(csv_path):
                try:
                    scan_and_supplement(csv_path, output_path, relation_backend, workers_final)
                except Exception as exc:
                    log_step(f"[WATCH] 最终补充失败: {type(exc).__name__}: {exc}")
                # 最后把 supplement 结果合并回 daily_output（覆盖 no_license）
                try:
                    merge_supplement_result(csv_path)
                except Exception as exc:
                    log_step(f"[WATCH] 合并 supplement 结果失败: {type(exc).__name__}: {exc}")
            if timed_out:
                log_step(f"[WATCH] 超过最大运行时长 {max_runtime}s，强制退出（防止 PID 复用导致永不退出）")
            else:
                log_step(f"[WATCH] 检测到 process_data 已退出，用 {workers_final} 并发完成最终补充后退出")
            return

        try:
            time.sleep(interval)
        except KeyboardInterrupt:
            log_step("[WATCH] 收到中断信号（Ctrl+C），退出")
            return


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="扫描 process_data_with_cpu_gitee_llm.py 结果 CSV，补充 maven 源 no_license")
    parser.add_argument("--input", "-i", required=True, help="daily_output/license_<date>.csv 路径（6 列无表头）")
    parser.add_argument("--output", "-o", default=None, help="输出 CSV（默认原地写回 --input）")
    parser.add_argument("--relation-backend", default=None, help="关系判断模型：deepseek（默认）或 local")
    parser.add_argument("--workers", type=int, default=8, help="并发线程数（watch 模式建议 1~2）")
    parser.add_argument("--workers-final", type=int, default=None, help="watch 模式 process_data 退出后的最终补充并发数（默认 10）")
    parser.add_argument("--watch", action="store_true", help="watch 循环模式：反复扫描回填，直到 process_data 退出")
    parser.add_argument("--parent-pid", type=int, default=None, help="process_data 的进程 PID（watch 模式用，检测它是否退出）")
    parser.add_argument("--interval", type=int, default=30, help="watch 模式每轮扫描间隔（秒）")
    parser.add_argument("--max-runtime", type=float, default=MAX_RUNTIME, help="watch 模式最大运行时长（秒），超时强制退出")
    parser.add_argument("--verbose", action="store_true", help="打开详细日志（每条记录的补充结果、爬取/网络细节）")
    args = parser.parse_args()

    if args.verbose:
        VERBOSE = True

    if args.watch:
        watch_and_supplement(args.input, args.parent_pid, args.output,
                             args.relation_backend, args.workers, args.workers_final, args.interval, args.max_runtime)
    else:
        clear_supplement_result(args.input)
        scan_and_supplement(args.input, args.output, args.relation_backend, args.workers)
        merge_supplement_result(args.input)
