# =========================================================
# 脚本功能与输出格式说明
# =========================================================
# 功能：从 Maven 仓库下载 POM 文件，沿 parent 链递归查找许可证，
#       输出每个组件的许可证信息（JSON 格式）。
#
# 输入：CSV 文件，必须含两列：
#   - purl     ：如 pkg:maven/group/artifact
#   - version  ：版本号
#
# 输出：JSON 数组，每个元素是一条组件记录，字段如下：
#   purl          原始 purl
#   version       版本号
#   license_text  许可证结果，取值可能是：
#                   - 许可证名称（单许可证，如 "MIT License"）
#                   - "<licenses>...</licenses>" 原始 XML（多许可证）
#                   - "no_license_404"   组件自身 POM 下载不到（404）
#                   - "no_license"       POM 找到但整条 parent 链无许可证
#                   - "xml_parse_error"  POM 解析失败
#                   - "cycle_detected"   parent 链出现循环
#                   - "invalid_purl"     purl 解析失败
#                   - "unexpected_error" 处理异常
#   license_source 结果来源/原因，取值可能是：
#                   - "pom_license"      在 POM 或 parent 链中找到许可证
#                   - "http_error"       组件自身 POM 404（depth=0）
#                   - "parent_not_found" parent 链上 POM 404（depth>0）
#                   - "no_parent"        无 <licenses> 且无 <parent>
#                   - "max_depth"        超过最大 parent 深度
#                   - "xml_error"        XML 解析失败
#                   - "parent_cycle"     parent 链循环
#                   - "parse_failed"     purl 解析失败
#                   - "exception"        处理异常
#   pom_text      POM 原始 XML 文本（单许可证时为空字符串；多许可证/错误态保留原文）
#   source        POM 下载地址（URL 或 file:/// 本地路径；下载失败时为空字符串）
#
# 命令行参数（argparse）：
#   --input/-i  输入 CSV 路径（默认 data/input.csv）
#   --output/-o 输出 JSON 路径（默认 data/output.json）
#   --proxy     代理地址（直连传空字符串 ""）
#   --workers   并发线程数（默认 50）
#   --quiet     关闭详细日志
# =========================================================

import os
import re
import time
import random
import hashlib
import json
import csv
import traceback
import threading
import argparse
import pandas as pd
import requests
import ijson

from bs4 import BeautifulSoup
from tqdm import tqdm
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock


# =========================================================
# 配置
# =========================================================

# 脚本所在目录（相对路径基于脚本位置，部署到服务器也不用改）
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

DATA_DIR = os.path.join(SCRIPT_DIR, "问题数据测试/0908")

INPUT_CSV = os.path.join(DATA_DIR, '0908_http_error.csv')

OUTPUT_JSON = os.path.join(DATA_DIR, '0908_http_error_output.csv')

CACHE_DIR = os.path.join(SCRIPT_DIR, "pom_cache")

MAX_PARENT_DEPTH = 30

REQUEST_TIMEOUT = 6     # 单次请求超时（秒），不要太大；not_found 包要遍历几十个仓库

SLEEP_MIN = 0.001         # 请求后最小休眠（秒）
SLEEP_MAX = 0.005       # 请求后最大休眠（秒）

SAVE_EVERY = 10

MAX_WORKERS = 30       # 并发线程数（太高会导致服务器重置连接）

VERBOSE = True      # 详细日志开关：False=安静模式（快），True=调试模式（打印爬取链路）

# Clash 代理地址（规则模式：国内直连、国外走 VPN）
# 不需要代理则设为空字符串 ""
PROXY_URL = "127.0.0.1:7897"

# 重试配置（减少重试次数以加速 not_found 包的处理）
RETRY_TOTAL = 1          # 最大重试次数（挂掉的仓库 1 秒超时就走，不再反复重试）
RETRY_BACKOFF = 0.5      # 退避因子（原 1）


# =========================================================‘
# 请求头
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


# =========================================================
# 36 个仓库，按优先级回退
# =========================================================

M2_LOCAL = os.path.join(
    os.path.expanduser("~"),
    ".m2",
    "repository"
)

REPOSITORIES = [
    # (name, base_url, is_local)
    # ── 本地缓存 ──
    ("M2_LOCAL",              M2_LOCAL,                                                         True),
    # ── Maven Central 主站（优先）──
    ("Maven_Central",         "https://repo1.maven.org/maven2",                                 False),
    ("Maven_Central_Alt",     "https://repo.maven.apache.org/maven2",                           False),
    # ── 国内镜像（快 + 聚合多源，可能有 JCenter 遗留缓存）──
    ("Aliyun",                "https://maven.aliyun.com/repository/public",                     False),
    ("Huawei",                "https://mirrors.huaweicloud.com/repository/maven",               False),
    ("Tencent",               "https://mirrors.cloud.tencent.com/repository/maven",             False),
    ("Tsinghua",              "https://mirrors.tuna.tsinghua.edu.cn/maven2",                    False),
    # ── 组织/企业仓库 ──
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
    # ── Snapshots ──
    ("Sonatype_Snapshots",    "https://oss.sonatype.org/content/repositories/snapshots",         False),
    ("Apache_Snapshots",      "https://repository.apache.org/snapshots",                        False),
]


# =========================================================
# 线程安全
# =========================================================

_thread_local = threading.local()

_cache_lock = Lock()

_print_lock = Lock()

_write_lock = Lock()


def _log(*args, **kwargs):
    """加锁打印，多线程不交织。VERBOSE=False 时直接跳过"""
    if not VERBOSE:
        return
    with _print_lock:
        print(*args, **kwargs)


def get_session():
    """每线程独立 Session"""
    if not hasattr(_thread_local, "session"):
        _thread_local.session = requests.Session()
        _thread_local.session.trust_env = False  # 忽略系统代理（避免 Clash 关闭后仍走死掉的代理）

        from requests.adapters import HTTPAdapter
        from urllib3.util.retry import Retry

        retry_strategy = Retry(
            total=RETRY_TOTAL,
            backoff_factor=RETRY_BACKOFF,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET"]
        )

        adapter = HTTPAdapter(
            max_retries=retry_strategy,
            pool_connections=20,
            pool_maxsize=20
        )

        _thread_local.session.mount("http://", adapter)
        _thread_local.session.mount("https://", adapter)
        _thread_local.session.headers.update(HEADERS)

        # 走 Clash 代理（规则模式：国内直连、国外自动走 VPN）
        if PROXY_URL:
            _thread_local.session.proxies = {
                "http": PROXY_URL,
                "https": PROXY_URL,
            }

    return _thread_local.session


# =========================================================
# 内存缓存
# =========================================================

MEMORY_CACHE = {}


# =========================================================
# 工具函数
# =========================================================

def clean_text(text):
    return re.sub(r"\s+", " ", str(text)).strip()


def safe_filename(text):
    md5 = hashlib.md5(text.encode()).hexdigest()
    return md5 + ".xml"


def ensure_cache_dir():
    os.makedirs(CACHE_DIR, exist_ok=True)


def random_sleep():
    time.sleep(random.uniform(SLEEP_MIN, SLEEP_MAX))


# =========================================================
# PURL 解析
# =========================================================

def parse_maven_purl(purl):
    """
    支持:
      pkg:maven/group/artifact
      pkg:maven/group/artifact@version
    """
    try:
        if pd.isna(purl):
            return None, None

        purl = str(purl).strip()

        if not purl.startswith("pkg:maven/"):
            return None, None

        body = purl.replace("pkg:maven/", "", 1)
        body = body.split("@")[0]
        body = body.split("?")[0]
        body = body.split("#")[0]

        parts = body.split("/")

        if len(parts) < 2:
            return None, None

        artifact_id = parts[-1]
        group_id = ".".join(parts[:-1])

        return group_id, artifact_id

    except Exception:
        return None, None


# =========================================================
# 构建 POM 路径
# =========================================================

def build_pom_path(group_id, artifact_id, version):
    """返回 POM 的相对路径部分"""
    group_path = group_id.replace(".", "/")
    return f"{group_path}/{artifact_id}/{version}/{artifact_id}-{version}.pom"


# =========================================================
# 内容校验（防 JFrog HTML 污染）
# =========================================================

def is_valid_pom_xml(text):
    """
    HTTP 200 后校验：内容中必须包含 <project 根元素。
    允许前置的 XML 声明、注释（某些 POM 以 <!-- 版权注释 --> 开头）。
    JFrog 等 HTML 页面以 <!DOCTYPE html> 开头，不含 <project，会被正确拒绝。
    """
    if not text:
        return False
    stripped = text.strip()
    # 在前 5000 字符内查找 <project 根元素
    return "<project" in stripped[:5000]


# =========================================================
# 下载 POM（19 仓库回退 + SNAPSHOT 回退）
# =========================================================

# =========================================================
# 仓库预筛选：根据 group_id 跳过不相关的仓库
# =========================================================

# 通用仓库（对所有包都尝试）
_GENERAL_REPOS = {
    "M2_LOCAL", "Aliyun", "Huawei", "Tencent", "Tsinghua",
    "Maven_Central", "Maven_Central_Alt",
}

# 专属仓库的 groupId 前缀匹配规则
# 注意：public/聚合仓库（Nexus "public" 组）代理了各源的包，必须置空（总是尝试）
_REPO_PREFIX_MAP = {
    "Atlassian_External":  [],   # public/聚合（代理外部包）
    "Atlassian":           ["com.atlassian."],
    "Atlassian_3rdParty":  [],   # 3rdparty（聚合）
    "Atlassian_Public":    [],   # maven-public（聚合）
    "Atlassian_New":       [],   # atlassian-public（聚合）
    "Spring":              ["org.springframework.", "org.springdoc."],
    "JBoss":               ["org.jboss.", "org.hibernate.", "org.jbpm.", "org.drools."],
    "JBoss_EA":            ["org.jboss.", "org.hibernate."],
    "WSO2":                ["org.wso2."],
    "WSO2_Public":         [],   # public（聚合）
    "Mulesoft":            [],   # public（聚合）
    "Hortonworks":         ["org.apache.hadoop.", "com.hortonworks."],
    "Cloudera":            [],   # public（聚合）
    "Confluent":           ["io.confluent.", "org.apache.kafka."],
    "Google":              ["com.google.", "com.android.", "androidx."],
    "Aliyun_Google":       ["com.google.", "com.android.", "androidx."],
    "XWiki":               ["org.xwiki."],
    "Gigaspaces":          ["org.openspaces.", "com.gigaspaces."],
    "KtorEAP":             ["io.ktor."],
    "Alfresco":            [],   # public（聚合）
    "BeDataDriven":        [],   # 小众仓库，仅通用仓库失败后才试
    "Gradle_Plugins":      [],   # Gradle 插件，无固定前缀
    "RedHat":              [],   # Red Hat 包前缀分散
    "Clojars":             [],   # Clojure 包，无固定前缀
    "OW2":                 [],   # OW2 中间件，无固定前缀
    "Sonatype_Releases":   [],   # 通用 OSS 发布仓库
    "Sonatype_Snapshots":  [],   # Snapshot 仓库
    "Apache_Snapshots":    [],   # Snapshot 仓库
    "JitPack":             [],   # GitHub 构建，无固定前缀
}


def _should_try_repo(repo_name, group_id):
    """判断是否需要尝试某个仓库（基于 groupId 前缀）"""
    if repo_name in _GENERAL_REPOS:
        return True

    prefixes = _REPO_PREFIX_MAP.get(repo_name)
    if prefixes is None:
        return True  # 未知仓库，默认尝试

    if not prefixes:
        return True  # 空列表 = 无前缀约束，总是尝试

    return any(group_id.startswith(p) for p in prefixes)


# 429 限流计数器（用于诊断是否被限流）
_rate_limit_hits = 0
_rate_limit_lock = Lock()


def _try_one_repo(repo_name, repo_base, is_local, pom_rel, cache_key, cache_path, url_path, try_version):
    """从单个仓库尝试获取 POM。成功返回 (xml_text, repo_name, try_version, source_url)，失败返回 None。"""
    # 本地 .m2
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

    # 远程仓库
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
        print(f"[ERROR] {repo_name}: {type(e).__name__}: {e}")
        return None


def fetch_pom(group_id, artifact_id, version, filter_repos=True, preferred_repo=None):
    """
    按优先级查询仓库，第一个命中且内容合法的即返回。
    两阶段：先过滤（快），全 miss 再兜底全试（完整，不遗漏）。
    preferred_repo: 优先尝试的仓库（parent 链用，去与子节点相同的仓库找）。
    对 SNAPSHOT 版本：404 后自动去掉 -SNAPSHOT 后缀重试。
    返回 (xml_text, repo_name, actual_version, source_url)
    """

    cache_key = f"{group_id}:{artifact_id}:{version}"

    # =====================================================
    # 内存缓存
    # =====================================================

    with _cache_lock:
        if cache_key in MEMORY_CACHE:
            return MEMORY_CACHE[cache_key]

    ensure_cache_dir()

    filename = safe_filename(cache_key)
    cache_path = os.path.join(CACHE_DIR, filename)
    url_path = cache_path + ".url"  # sidecar：记录下载 URL

    # =====================================================
    # 磁盘缓存
    # =====================================================

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

    # =====================================================
    # 多仓库回退
    # =====================================================

    versions_to_try = [version]

    # SNAPSHOT 回退：8.7.2-SNAPSHOT → 8.7.2
    is_snapshot = str(version).upper().endswith("-SNAPSHOT")
    if is_snapshot:
        release_ver = str(version)[: -len("-SNAPSHOT")]
        if release_ver:
            versions_to_try.append(release_ver)

    for try_version in versions_to_try:
        pom_rel = build_pom_path(group_id, artifact_id, try_version)

        tried = set()

        # ---- Step 0：优先尝试 preferred_repo（与子节点相同仓库） ----
        if preferred_repo:
            for repo_name, repo_base, is_local in REPOSITORIES:
                if repo_name == preferred_repo:
                    tried.add(repo_name)
                    result = _try_one_repo(repo_name, repo_base, is_local, pom_rel, cache_key, cache_path, url_path, try_version)
                    if result is not None:
                        return result
                    break

        # ---- 第一阶段：过滤后的仓库（快） ----
        for repo_name, repo_base, is_local in REPOSITORIES:
            if repo_name in tried:
                continue
            if filter_repos and not _should_try_repo(repo_name, group_id):
                continue
            tried.add(repo_name)
            result = _try_one_repo(repo_name, repo_base, is_local, pom_rel, cache_key, cache_path, url_path, try_version)
            if result is not None:
                return result

        # ---- 第二阶段：兜底（全试被过滤掉的仓库，保证不遗漏） ----
        if filter_repos:
            for repo_name, repo_base, is_local in REPOSITORIES:
                if repo_name in tried:
                    continue
                result = _try_one_repo(repo_name, repo_base, is_local, pom_rel, cache_key, cache_path, url_path, try_version)
                if result is not None:
                    return result

    # 全部失败
    with _cache_lock:
        MEMORY_CACHE[cache_key] = (None, None, version, None)
    return MEMORY_CACHE[cache_key]


# =========================================================
# 提取 licenses
# =========================================================

# =========================================================
# POM 容错解析（处理 HTML 实体等非法 XML 字符）
# =========================================================

# 常见 HTML 实体 → XML 兼容的数字字符引用
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
    """
    容错 XML 解析：
    1. 先尝试严格 XML 解析
    2. 失败则替换常见 HTML 实体后重试
    3. 仍失败则抛异常，由上层标记 xml_parse_error
    """
    # 第一次尝试：严格 XML
    try:
        return BeautifulSoup(xml_text, "xml")
    except Exception:
        pass

    # 第二次尝试：替换 HTML 实体
    fixed = xml_text
    for entity, replacement in _HTML_ENTITIES.items():
        fixed = fixed.replace(entity, replacement)

    # 如果替换了实体，再试一次
    if fixed != xml_text:
        try:
            return BeautifulSoup(fixed, "xml")
        except Exception:
            pass

    raise


def extract_licenses(xml_text):
    """
    提取整个 <licenses> 标签，原封不动返回（含原始换行与缩进）。
    无 licenses 时返回 None。
    """
    m = re.search(
        r'<licenses\b[^>]*>.*?</licenses>',
        xml_text,
        re.DOTALL
    )
    if not m:
        return None

    return m.group(0)


def is_multi_license_name(name):
    """
    判断单个 name 是否含多个许可证（+ 连接）。
    "CDDL+GPL" → True（两个许可证）
    "GPL-2.0+" → False（"+" 表示"或更高版本"，不是多许可证）
    规则: + 前后都有字母（许可证名），才判定为多许可证。
    """
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
    """
    处理 license_text:
      1. 多许可证 → 原样返回 POM 原文
         a. 有多个 <license> 标签
         b. 单个 <license> 标签的 <name> 中用 + 连接多个许可证
      2. 单许可证 → 直接返回许可证名称
    """
    if not license_text or not license_text.strip().startswith("<licenses"):
        # 错误状态（no_license / pom_not_found / invalid_purl / 空）原样返回
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
        # 多许可证 → 原样返回 POM 原文
        return license_text
    elif tag_count == 1:
        # 单许可证 → 返回许可证名称
        return names[0]
    else:
        # 没有 license 标签 → 原样返回
        return license_text


# =========================================================
# 提取 parent
# =========================================================

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
        "version": clean_text(version_tag.text)
    }


# =========================================================
# 递归解析 License
# =========================================================

def resolve_license(group_id, artifact_id, version):

    visited = set()

    current_group = group_id
    current_artifact = artifact_id
    current_version = version

    depth = 0
    hit_chain = []  # 记录命中仓库链路

    # 记录"最近一次成功下载"这一层的 POM 文本与下载链接
    # license 在哪一层找到，pom_text / source 就保留哪一层
    current_pom_text = ""
    current_source_url = ""

    # 记录当前 POM 的来源仓库，供 parent 链优先去同一仓库找
    current_repo = None

    while depth < MAX_PARENT_DEPTH:

        current_key = (current_group, current_artifact, current_version)

        # =================================================
        # 防循环
        # =================================================

        if current_key in visited:
            _log(f"[RESULT] depth={depth} | {current_group}:{current_artifact}:{current_version} "
                 f"→ cycle_detected / parent_cycle")
            return {
                "license": "cycle_detected",
                "license_source": "parent_cycle",
                "pom_text": current_pom_text,
                "source": current_source_url,
            }

        visited.add(current_key)

        # =================================================
        # 获取 POM
        # =================================================

        xml_text, repo_name, actual_version, source_url = fetch_pom(
            current_group,
            current_artifact,
            current_version,
            preferred_repo=current_repo
        )

        # 记录当前层的 POM 文本与下载链接（仅当下载成功时更新，失败则保留上一层的值）
        if xml_text:
            current_pom_text = xml_text
            current_source_url = source_url or ""
            # 记录来源仓库，供 parent 链优先去同一仓库找（disk_cache 不是真实仓库，跳过）
            if repo_name and repo_name != "disk_cache":
                current_repo = repo_name

        if not xml_text:
            # 只有组件自身 POM 找不到(depth=0) → no_license_404
            # parent 链上 POM 找不到(depth>0) → no_license
            if depth == 0:
                _log(f"[RESULT] depth={depth} | {current_group}:{current_artifact}:{current_version} "
                     f"→ no_license_404 / http_error")
                return {
                    "license": "no_license_404",
                    "license_source": "http_error",
                    "pom_text": current_pom_text,
                    "source": current_source_url,
                }
            else:
                chain_str = " → ".join(hit_chain)
                _log(f"[RESULT] depth={depth} | {current_group}:{current_artifact}:{current_version} "
                     f"→ no_license / parent_not_found | chain: {chain_str}")
                return {
                    "license": "no_license",
                    "license_source": "parent_not_found",
                    "pom_text": current_pom_text,
                    "source": current_source_url,
                }

        hit_chain.append(f"{repo_name}@{actual_version}")

        # =================================================
        # 解析 XML
        # =================================================

        try:
            soup = parse_pom_xml(xml_text)
        except Exception:
            _log(f"[RESULT] depth={depth} | {current_group}:{current_artifact}:{current_version} "
                 f"→ xml_parse_error / xml_error")
            return {
                "license": "xml_parse_error",
                "license_source": "xml_error",
                "pom_text": current_pom_text,
                "source": current_source_url,
            }

        # =================================================
        # 当前 licenses
        # =================================================

        licenses = extract_licenses(xml_text)

        if licenses:
            chain_str = " → ".join(hit_chain)
            _log(f"[RESULT] depth={depth} | {current_group}:{current_artifact}:{current_version} "
                 f"→ license={licenses} / pom_license | chain: {chain_str}")
            return {
                "license": licenses,
                "license_source": "pom_license",
                "pom_text": current_pom_text,
                "source": current_source_url,
            }

        # =================================================
        # 向上查 parent
        # =================================================

        parent = extract_parent(soup)

        if not parent:
            _log(f"[RESULT] depth={depth} | {current_group}:{current_artifact}:{current_version} "
                 f"→ no_license / no_parent | chain: {' → '.join(hit_chain)}")
            return {
                "license": "no_license",
                "license_source": "no_parent",
                "pom_text": current_pom_text,
                "source": current_source_url,
            }

        current_group = parent["group_id"]
        current_artifact = parent["artifact_id"]
        current_version = parent["version"]

        depth += 1

    _log(f"[RESULT] depth={depth} | {current_group}:{current_artifact}:{current_version} "
         f"→ no_license / max_depth")
    return {
        "license": "no_license",
        "license_source": "max_depth",
        "pom_text": current_pom_text,
        "source": current_source_url,
    }


# =========================================================
# 处理单条记录（线程池用）
# =========================================================

def process_one_row(purl, version):
    """处理单条 PURL，返回 JSON 对象（dict）"""

    try:
        group_id, artifact_id = parse_maven_purl(purl)

        if not group_id or not artifact_id:
            return {
                "purl": purl,
                "version": version,
                "license_text": "invalid_purl",
                "license_source": "parse_failed",
                "pom_text": "",
                "source": "",
            }

        result = resolve_license(group_id, artifact_id, version)

        license_text = process_license_text(result["license"])

        # 单许可证：清空 pom_text（与 remove_single_license_pomtext.py 逻辑一致）
        #   多许可证 / 错误态：保留 pom_text
        pom_text = result["pom_text"]
        if result["license_source"] == "pom_license" and not license_text.strip().startswith("<licenses"):
            pom_text = ""

        return {
            "purl": purl,
            "version": version,
            "license_text": license_text,
            "license_source": result["license_source"],
            "pom_text": pom_text,
            "source": result["source"],
        }

    except Exception as e:
        _log(f"[ERROR] {purl} {version} | {str(e)}")
        traceback.print_exc()
        return {
            "purl": purl,
            "version": version,
            "license_text": "unexpected_error",
            "license_source": "exception",
            "pom_text": "",
            "source": "",
        }


# =========================================================
# 主流程
# =========================================================

def _load_existing_results_tolerant(filepath):
    """
    流式读取已有的输出 JSON（用 ijson，低内存，适合大文件）。
    返回 (existing_results, processed) 元组。
    文件截断/崩溃时，使用已读取的完整对象（截断的半截对象自动丢弃）。
    """
    results = []
    processed = set()

    try:
        with open(filepath, "rb") as f:
            # 流式逐个读取对象，不一次性加载整个文件
            for obj in ijson.items(f, "item"):
                results.append(obj)
                purl = str(obj.get("purl", "")).strip()
                version = str(obj.get("version", "")).strip()
                if purl and version:
                    processed.add(f"{purl}@{version}")
    except ijson.common.IncompleteJSONError:
        # 文件截断（崩溃残留），使用已读取的完整对象
        pass
    except Exception:
        # 文件损坏等其他错误，从头开始
        results = []
        processed = set()

    return results, processed


def process_csv():

    df = pd.read_csv(INPUT_CSV)
    total = len(df)

    # =====================================================
    # 已处理记录（断点续跑，容错截断）
    # =====================================================

    processed = set()
    existing_results = []

    if os.path.exists(OUTPUT_JSON):
        existing_results, processed = _load_existing_results_tolerant(OUTPUT_JSON)
        if processed:
            _log(f"[RESUME] {len(processed)} already processed")
        else:
            _log("[RESUME] 未找到已处理记录（文件为空或截断），从头开始")

    # =====================================================
    # 构建待处理列表
    # =====================================================

    tasks = []

    skipped_no_license = 0

    for _, row in df.iterrows():
        purl = clean_text(row.get("purl", ""))
        version = clean_text(row.get("version", ""))
        license_text = clean_text(row.get("license_text", ""))

        if not purl or not version:
            continue

        # license_text 已是 no_license（POM 存在但整条 parent 链无许可证）：
        # 重爬也还是 no_license，纯浪费请求，直接跳过、不再抓取 pom_text
        if license_text == "no_license":
            skipped_no_license += 1
            continue

        unique_key = f"{purl}@{version}"

        if unique_key in processed:
            continue

        tasks.append((purl, version))

    if skipped_no_license:
        _log(f"[SKIP] {skipped_no_license} 条 license_text=no_license 已跳过（不再爬取 pom_text）")

    _log(f"[TASKS] {len(tasks)} to process out of {total} total")

    if not tasks:
        _log("DONE - nothing to process")
        return

    # =====================================================
    # 写入开头 '[' + 已有结果（一次性）
    # =====================================================

    def _fmt(obj):
        """对象转格式化 JSON，缩进对齐数组层级"""
        s = json.dumps(obj, indent=2, ensure_ascii=False)
        return "  " + s.replace("\n", "\n  ")

    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        f.write("[\n")
        for obj in existing_results:
            f.write(_fmt(obj) + ",\n")

    # =====================================================
    # 线程池并发 + 增量写入（每 SAVE_EVERY 条落盘）
    # =====================================================

    completed_since_save = 0
    rows_buffer = []

    with tqdm(total=len(tasks), desc="Processing") as pbar:
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:

            futures = {
                executor.submit(process_one_row, purl, version): (purl, version)
                for purl, version in tasks
            }

            for future in as_completed(futures):
                try:
                    row_obj = future.result()
                    rows_buffer.append(row_obj)

                except Exception as e:
                    purl, version = futures[future]
                    _log(f"[FUTURE ERROR] {purl} {version}: {str(e)}")
                    rows_buffer.append({
                        "purl": purl,
                        "version": version,
                        "license_text": "unexpected_error",
                        "license_source": "exception",
                        "pom_text": "",
                        "source": "",
                    })

                completed_since_save += 1

                # 每 SAVE_EVERY 条批量写入（格式化 JSON）
                if completed_since_save >= SAVE_EVERY:
                    with _write_lock:
                        with open(OUTPUT_JSON, "a", encoding="utf-8") as f:
                            for obj in rows_buffer:
                                f.write(_fmt(obj) + ",\n")
                    rows_buffer.clear()
                    completed_since_save = 0

                pbar.update(1)

            # 写剩余
            if rows_buffer:
                with _write_lock:
                    with open(OUTPUT_JSON, "a", encoding="utf-8") as f:
                        for obj in rows_buffer:
                            f.write(_fmt(obj) + ",\n")

    # =====================================================
    # 结尾：去掉末尾逗号，补上 ']'
    # =====================================================

    with open(OUTPUT_JSON, "r", encoding="utf-8") as f:
        content = f.read()
    content = content.rstrip().rstrip(",")
    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        f.write(content + "\n]\n")

    _log("\nDONE")


# =========================================================
# MAIN
# =========================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="抓取 Maven POM 并递归解析许可证")
    parser.add_argument("--input", "-i", default=INPUT_CSV, help="输入 CSV 路径（含 purl、version 列）")
    parser.add_argument("--output", "-o", default=OUTPUT_JSON, help="输出 JSON 路径")
    parser.add_argument("--proxy", default=PROXY_URL, help="代理地址；服务器直连外网时传空字符串 ''")
    parser.add_argument("--workers", type=int, default=MAX_WORKERS, help="并发线程数")
    parser.add_argument("--quiet", action="store_true", help="关闭详细日志（VERBOSE=False）")
    args = parser.parse_args()

    INPUT_CSV = args.input
    OUTPUT_JSON = args.output
    PROXY_URL = args.proxy
    MAX_WORKERS = args.workers
    if args.quiet:
        VERBOSE = False

    # 确保输出目录存在（脚本原本不会自动创建输出目录）
    out_dir = os.path.dirname(OUTPUT_JSON)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    process_csv()
