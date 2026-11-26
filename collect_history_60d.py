"""有界、低频、可恢复的 60 日社区历史采集器。

用途
====
这个脚本只采集匿名可访问的东方财富股吧与新浪股吧公开帖子，不登录、
不使用代理、不更换 IP、不处理验证码，也不尝试绕过站点返回的身份核实。
同花顺的公开热度流不能可靠地按日期回溯，因此这里有意不接入。

默认保留 60 个自然日基础跨度并额外多取 30 日档案余量，使后续构建器通常
能够覆盖最近 60 个 A 股交易日。论坛原始帖子仍按发布日期完整归档，但评分
只使用交易日文本与最近三个交易日窗口。采集结果写入 SQLite；帖子、
来源游标、运行记录与逐请求审计信息会在同一个事务中保存，所以中断后可
直接重复运行并从断点继续。

安全默认值
----------
* 单线程；任意两次请求至少间隔 1.5 秒（命令行最小允许 1 秒）。
* 每次运行最多 20 次网络请求；可按需显式提高。
* 默认代表性采样：东方财富每源最多 14 个指数页，新浪每源最多 50 次请求。
* 403、429、验证码或“身份核实”会立即停止该平台在本轮的后续请求。
* 不保存昵称和用户 ID，只保存不可逆作者哈希、帖子标题、时间与公开互动数。

常用命令
--------
先看计划（不联网、不写文件）：
    python collect_history_60d.py --dry-run

两来源、两请求的小样本验证（不落盘）：
    python collect_history_60d.py --probe

正式开始或继续上次采集：
    python collect_history_60d.py --max-requests 20

只重建覆盖报告，不联网：
    python collect_history_60d.py --report-only

注意：公开可访问不等于获得了批量再利用授权。运行者仍需自行确认站点条款、
robots 规则、版权和个人信息处理要求；本工具不会把结果宣称为官方授权数据。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sqlite3
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time as datetime_time, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

import update_index as core


ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
DEFAULT_DB_PATH = DATA_DIR / "history_60d.sqlite3"
DEFAULT_REPORT_PATH = DATA_DIR / "history_60d_report.json"
SINA_BID_CACHE_PATH = DATA_DIR / "sina_bid_cache.json"
CN_TZ = timezone(timedelta(hours=8))
SCHEMA_VERSION = 1
COLLECTOR_VERSION = "1.0"
PLATFORMS = {"eastmoney", "sina"}
EASTMONEY_END_MESSAGES = {"到底啦"}
PLATFORM_NAMES = {
    "eastmoney": "东方财富股吧",
    "sina": "新浪股吧",
}
USER_AGENT = (
    "Mozilla/5.0 (compatible; BMSH-HistoryCollector/1.0; "
    "+local-personal-research)"
)
BLOCK_MARKERS = (
    "身份核实",
    "访问过于频繁",
    "请输入验证码",
    "安全验证",
    "系统检测到您的请求存在异常",
)


class RequestBudgetExhausted(RuntimeError):
    """本轮请求预算已经用完。"""


class FetchFailure(RuntimeError):
    """携带可审计请求信息的单次请求失败。"""

    def __init__(
        self,
        message: str,
        *,
        url: str,
        requested_at: str,
        elapsed_ms: int,
        http_status: Optional[int] = None,
        response_bytes: int = 0,
        response_sha256: str = "",
        stop_platform: bool = False,
    ) -> None:
        super().__init__(message)
        self.url = url
        self.requested_at = requested_at
        self.elapsed_ms = elapsed_ms
        self.http_status = http_status
        self.response_bytes = response_bytes
        self.response_sha256 = response_sha256
        self.stop_platform = stop_platform


@dataclass(frozen=True)
class SourceSpec:
    key: str
    platform: str
    code: str
    name: str
    sectors: Tuple[str, ...]


@dataclass
class NetworkResponse:
    url: str
    text: str
    requested_at: str
    elapsed_ms: int
    http_status: int
    response_bytes: int
    response_sha256: str


@dataclass
class ParsedPage:
    request_kind: str
    cursor: Dict[str, object]
    rows_returned: int
    posts: List[Dict[str, object]]
    all_times: List[datetime]
    next_page: Optional[int] = None
    next_cursor_value: Optional[str] = None
    next_cursor_tid: Optional[str] = None
    discovered_external_id: Optional[str] = None
    total_count: Optional[int] = None
    empty_is_complete: bool = True


def now_cn() -> datetime:
    return datetime.now(CN_TZ)


def iso_now() -> str:
    return now_cn().isoformat(timespec="seconds")


def json_compact(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def atomic_write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(path.name + ".tmp")
    temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temp_path.replace(path)


def split_csv(values: Optional[Sequence[str]]) -> Set[str]:
    result: Set[str] = set()
    for value in values or []:
        result.update(part.strip() for part in str(value).split(",") if part.strip())
    return result


def parse_as_of(value: Optional[str]) -> datetime:
    if not value:
        return now_cn()
    text = value.strip()
    # datetime.fromisoformat("YYYY-MM-DD") 会返回当天零点，所以必须先识别
    # 纯日期。纯日期表示该自然日完整日终，不能静默漏掉当天绝大多数帖子。
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        try:
            parsed_date = date.fromisoformat(text)
        except ValueError as exc:
            raise ValueError("--as-of 需要 YYYY-MM-DD 或 ISO 日期时间") from exc
        return datetime.combine(parsed_date, datetime_time.max, tzinfo=CN_TZ)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError("--as-of 需要 YYYY-MM-DD 或 ISO 日期时间") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=CN_TZ)
    return parsed.astimezone(CN_TZ)


def author_hash(platform: str, value: object) -> str:
    raw = f"{platform}:{str(value or '').strip()}".encode("utf-8", errors="replace")
    return hashlib.sha256(raw).hexdigest()[:24]


def read_json(path: Path, default: object) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def build_sources(sina_stocks_per_sector: int = 1) -> Tuple[List[SourceSpec], Dict[str, str]]:
    raw_specs, market_keys, sector_source_keys = core.build_source_specs()
    source_sectors: Dict[str, List[str]] = defaultdict(list)
    for key in market_keys:
        if key in raw_specs and raw_specs[key].get("platform") in PLATFORMS:
            source_sectors[key].append("__market__")
    for sector_key, keys in sector_source_keys.items():
        configured_sina = list(
            core.SECTORS[sector_key].get("sinaStocks")
            or core.SECTORS[sector_key].get("sourceStocks")
            or core.SECTORS[sector_key].get("stocks")
            or []
        )
        allowed_sina = {
            f"sina:{code}"
            for code, _ in configured_sina[:max(1, min(2, int(sina_stocks_per_sector)))]
        }
        for key in keys:
            if (
                key in raw_specs
                and raw_specs[key].get("platform") in PLATFORMS
                and (raw_specs[key].get("platform") != "sina" or key in allowed_sina)
            ):
                source_sectors[key].append(sector_key)

    specs: List[SourceSpec] = []
    for key, raw in raw_specs.items():
        platform = str(raw.get("platform") or "")
        if platform not in PLATFORMS:
            continue
        sectors = tuple(sorted(set(source_sectors.get(key) or ())))
        if not sectors:
            continue
        specs.append(SourceSpec(
            key=str(key),
            platform=platform,
            code=str(raw.get("code") or ""),
            name=str(raw.get("name") or raw.get("code") or key),
            sectors=sectors,
        ))

    sector_names = {key: str(value["name"]) for key, value in core.SECTORS.items()}
    sector_names["__market__"] = "全市场公共来源"
    return specs, sector_names


def filter_sources(specs: Sequence[SourceSpec], args: argparse.Namespace) -> List[SourceSpec]:
    platforms = split_csv(args.platforms) or set(PLATFORMS)
    unknown_platforms = platforms - PLATFORMS
    if unknown_platforms:
        raise ValueError(f"未知平台：{', '.join(sorted(unknown_platforms))}")
    requested_sources = split_csv(args.sources)
    requested_sectors = split_csv(args.sectors)
    known_keys = {spec.key for spec in specs}
    unknown_sources = requested_sources - known_keys
    if unknown_sources:
        raise ValueError(f"未知来源：{', '.join(sorted(unknown_sources))}")
    known_sectors = set(core.SECTORS) | {"__market__"}
    unknown_sectors = requested_sectors - known_sectors
    if unknown_sectors:
        raise ValueError(f"未知板块：{', '.join(sorted(unknown_sectors))}")

    selected = []
    for spec in specs:
        if spec.platform not in platforms:
            continue
        if requested_sources and spec.key not in requested_sources:
            continue
        if requested_sectors and not requested_sectors.intersection(spec.sectors):
            continue
        selected.append(spec)
    return selected


def connect_db(path_value: str) -> sqlite3.Connection:
    if path_value == ":memory:":
        conn = sqlite3.connect(":memory:")
    else:
        path = Path(path_value).resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if path_value != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS sources (
            source_key TEXT PRIMARY KEY,
            platform TEXT NOT NULL,
            code TEXT NOT NULL,
            name TEXT NOT NULL,
            sectors_json TEXT NOT NULL,
            external_id TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            next_page INTEGER NOT NULL DEFAULT 1,
            cursor_value TEXT,
            cursor_tid TEXT,
            sample_index INTEGER NOT NULL DEFAULT 0,
            past_cutoff_pages INTEGER NOT NULL DEFAULT 0,
            total_requests INTEGER NOT NULL DEFAULT 0,
            rows_seen INTEGER NOT NULL DEFAULT 0,
            posts_saved INTEGER NOT NULL DEFAULT 0,
            consecutive_errors INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            oldest_seen TEXT,
            newest_seen TEXT,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS posts (
            platform TEXT NOT NULL,
            source_key TEXT NOT NULL,
            post_id TEXT NOT NULL,
            source_code TEXT NOT NULL,
            source_name TEXT NOT NULL,
            published_at TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            title TEXT NOT NULL,
            author_hash TEXT NOT NULL,
            comments INTEGER NOT NULL DEFAULT 0,
            clicks INTEGER NOT NULL DEFAULT 0,
            likes INTEGER NOT NULL DEFAULT 0,
            kind TEXT NOT NULL DEFAULT 'post',
            PRIMARY KEY (source_key, post_id),
            FOREIGN KEY (source_key) REFERENCES sources(source_key)
        );
        CREATE INDEX IF NOT EXISTS idx_posts_published ON posts(published_at);
        CREATE INDEX IF NOT EXISTS idx_posts_platform_date ON posts(platform, published_at);

        CREATE VIEW IF NOT EXISTS estimated_posts AS
        SELECT platform, source_key, post_id, source_code, source_name,
               published_at, observed_at AS collected_at, title, author_hash,
               0 AS comments, 0 AS clicks, 0 AS likes, kind,
               comments AS observed_comments,
               clicks AS observed_clicks,
               likes AS observed_likes
        FROM posts;

        CREATE TABLE IF NOT EXISTS post_sectors (
            source_key TEXT NOT NULL,
            post_id TEXT NOT NULL,
            sector_key TEXT NOT NULL,
            PRIMARY KEY (source_key, post_id, sector_key),
            FOREIGN KEY (source_key, post_id) REFERENCES posts(source_key, post_id)
        );
        CREATE INDEX IF NOT EXISTS idx_post_sectors_sector ON post_sectors(sector_key);

        CREATE TABLE IF NOT EXISTS runs (
            run_id TEXT PRIMARY KEY,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            status TEXT NOT NULL,
            args_json TEXT NOT NULL,
            requests INTEGER NOT NULL DEFAULT 0,
            rows_seen INTEGER NOT NULL DEFAULT 0,
            posts_inserted INTEGER NOT NULL DEFAULT 0,
            errors INTEGER NOT NULL DEFAULT 0,
            note TEXT
        );

        CREATE TABLE IF NOT EXISTS audit (
            audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            request_no INTEGER NOT NULL,
            source_key TEXT NOT NULL,
            platform TEXT NOT NULL,
            request_kind TEXT NOT NULL,
            cursor_json TEXT NOT NULL,
            url TEXT NOT NULL,
            requested_at TEXT NOT NULL,
            elapsed_ms INTEGER NOT NULL,
            http_status INTEGER,
            result TEXT NOT NULL,
            rows_returned INTEGER NOT NULL DEFAULT 0,
            posts_accepted INTEGER NOT NULL DEFAULT 0,
            posts_inserted INTEGER NOT NULL DEFAULT 0,
            oldest_returned TEXT,
            newest_returned TEXT,
            response_bytes INTEGER NOT NULL DEFAULT 0,
            response_sha256 TEXT,
            message TEXT,
            FOREIGN KEY (run_id) REFERENCES runs(run_id)
        );
        CREATE INDEX IF NOT EXISTS idx_audit_run ON audit(run_id, request_no);
        CREATE INDEX IF NOT EXISTS idx_audit_source ON audit(source_key, audit_id);
        """
    )
    source_columns = {
        str(row[1]) for row in conn.execute("PRAGMA table_info(sources)").fetchall()
    }
    if "sample_index" not in source_columns:
        conn.execute(
            "ALTER TABLE sources ADD COLUMN sample_index INTEGER NOT NULL DEFAULT 0"
        )
    # 旧采集器曾把东方财富正常的“到底啦”终止响应误记为解析失败。
    # 只修复这个精确、可审计的状态，不触碰其他真实网络或解析错误。
    conn.execute(
        """
        UPDATE sources
        SET status = 'complete', consecutive_errors = 0, last_error = NULL,
            updated_at = ?
        WHERE platform = 'eastmoney' AND last_error = ?
        """,
        (iso_now(), next(iter(EASTMONEY_END_MESSAGES))),
    )
    set_meta(conn, "schema_version", str(SCHEMA_VERSION))
    set_meta(conn, "collector_version", COLLECTOR_VERSION)
    set_meta(conn, "interaction_counts_observed_at_collection", "true")
    set_meta(conn, "estimated_scoring_ignore_interactions_default", "true")
    set_meta(conn, "estimated_scoring_view", "estimated_posts")
    conn.commit()


def get_meta(conn: sqlite3.Connection, key: str) -> Optional[str]:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return str(row[0]) if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: object) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )


def resolve_window(
    conn: sqlite3.Connection,
    args: argparse.Namespace,
) -> Tuple[datetime, datetime, date]:
    existing_as_of = get_meta(conn, "window_as_of")
    existing_days = get_meta(conn, "score_days")
    existing_context = get_meta(conn, "context_days")
    if existing_as_of:
        as_of = datetime.fromisoformat(existing_as_of).astimezone(CN_TZ)
        if args.as_of:
            requested = parse_as_of(args.as_of)
            legacy_midnight_same_date = (
                as_of.time() == datetime_time.min
                and bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(args.as_of).strip()))
                and requested.date() == as_of.date()
            )
            if requested.isoformat() != as_of.isoformat() and not legacy_midnight_same_date:
                raise ValueError(
                    "数据库已绑定另一个 --as-of；请继续使用原窗口，或换一个新的 --db"
                )
        if existing_days and int(existing_days) != args.days:
            raise ValueError("数据库中的 --days 与本次不一致；请使用原参数或新数据库")
        if existing_context and int(existing_context) != args.context_days:
            raise ValueError("数据库中的 --context-days 与本次不一致；请使用原参数或新数据库")
    else:
        as_of = parse_as_of(args.as_of)
        set_meta(conn, "window_as_of", as_of.isoformat())
        set_meta(conn, "score_days", args.days)
        set_meta(conn, "context_days", args.context_days)

    score_start = as_of.date() - timedelta(days=args.days - 1)
    raw_cutoff_date = score_start - timedelta(days=args.context_days)
    raw_cutoff = datetime.combine(raw_cutoff_date, datetime_time.min, tzinfo=CN_TZ)
    set_meta(conn, "score_start", score_start.isoformat())
    set_meta(conn, "raw_cutoff", raw_cutoff.isoformat())
    existing_strategy = get_meta(conn, "collection_strategy")
    existing_spacing = get_meta(conn, "sina_target_spacing_days")
    if existing_strategy and existing_strategy != args.strategy:
        raise ValueError("数据库中的 --strategy 与本次不一致；请使用原参数或新数据库")
    if existing_spacing and int(existing_spacing) != args.sina_target_spacing:
        raise ValueError(
            "数据库中的 --sina-target-spacing 与本次不一致；请使用原参数或新数据库"
        )
    set_meta(conn, "collection_strategy", args.strategy)
    set_meta(conn, "sina_target_spacing_days", args.sina_target_spacing)
    set_meta(conn, "eastmoney_source_request_cap", args.eastmoney_page_cap)
    set_meta(conn, "sina_source_request_cap", args.sina_page_cap)
    conn.commit()
    return as_of, raw_cutoff, score_start


def initialize_sources(
    conn: sqlite3.Connection,
    specs: Sequence[SourceSpec],
    retry_blocked: bool,
) -> None:
    cached_bids = read_json(SINA_BID_CACHE_PATH, {})
    if not isinstance(cached_bids, dict):
        cached_bids = {}
    stamp = iso_now()
    for spec in specs:
        conn.execute(
            """
            INSERT OR IGNORE INTO sources(
                source_key, platform, code, name, sectors_json, external_id, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?)
            """,
            (
                spec.key,
                spec.platform,
                spec.code,
                spec.name,
                json_compact(spec.sectors),
                str(cached_bids.get(spec.code) or "") or None
                if spec.platform == "sina" else None,
                stamp,
            ),
        )
        conn.execute(
            """
            UPDATE sources
            SET platform = ?, code = ?, name = ?, sectors_json = ?,
                external_id = COALESCE(external_id, ?), updated_at = ?
            WHERE source_key = ?
            """,
            (
                spec.platform,
                spec.code,
                spec.name,
                json_compact(spec.sectors),
                str(cached_bids.get(spec.code) or "") or None
                if spec.platform == "sina" else None,
                stamp,
                spec.key,
            ),
        )
    if retry_blocked:
        keys = [spec.key for spec in specs]
        placeholders = ",".join("?" for _ in keys)
        if keys:
            conn.execute(
                f"UPDATE sources SET status = 'error', consecutive_errors = 0 "
                f"WHERE status = 'blocked' AND source_key IN ({placeholders})",
                keys,
            )
    conn.commit()


class LowFrequencyClient:
    def __init__(self, *, max_requests: int, delay: float, timeout: int) -> None:
        self.max_requests = max_requests
        self.delay = max(1.0, float(delay))
        self.timeout = timeout
        self.request_count = 0
        self._last_request_started = 0.0

    def get(self, url: str, platform: str) -> NetworkResponse:
        if self.request_count >= self.max_requests:
            raise RequestBudgetExhausted("本轮网络请求预算已用完")
        wait_seconds = self.delay - (time.monotonic() - self._last_request_started)
        if wait_seconds > 0:
            time.sleep(wait_seconds)

        requested_at = iso_now()
        started = time.monotonic()
        self._last_request_started = started
        self.request_count += 1
        referers = {
            "eastmoney": "https://guba.eastmoney.com/",
            "sina": "https://guba.sina.cn/",
        }
        headers = {
            "User-Agent": USER_AGENT,
            "Referer": referers[platform],
            "Accept": "application/json,text/html;q=0.8,*/*;q=0.5",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Accept-Encoding": "identity",
            "Connection": "close",
        }
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read(12 * 1024 * 1024 + 1)
                status = int(response.getcode() or 200)
                charset = response.headers.get_content_charset() or "utf-8"
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read(512 * 1024)
            except OSError:
                body = b""
            elapsed = int((time.monotonic() - started) * 1000)
            digest = hashlib.sha256(body).hexdigest() if body else ""
            raise FetchFailure(
                f"HTTP {exc.code}",
                url=url,
                requested_at=requested_at,
                elapsed_ms=elapsed,
                http_status=int(exc.code),
                response_bytes=len(body),
                response_sha256=digest,
                stop_platform=int(exc.code) in {403, 429},
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            elapsed = int((time.monotonic() - started) * 1000)
            raise FetchFailure(
                f"网络读取失败：{exc}",
                url=url,
                requested_at=requested_at,
                elapsed_ms=elapsed,
            ) from exc

        elapsed = int((time.monotonic() - started) * 1000)
        if len(body) > 12 * 1024 * 1024:
            digest = hashlib.sha256(body).hexdigest()
            raise FetchFailure(
                "响应超过 12 MiB 安全上限",
                url=url,
                requested_at=requested_at,
                elapsed_ms=elapsed,
                http_status=status,
                response_bytes=len(body),
                response_sha256=digest,
                stop_platform=True,
            )
        try:
            text = body.decode(charset)
        except (LookupError, UnicodeDecodeError):
            text = body.decode("utf-8", errors="replace")
        digest = hashlib.sha256(body).hexdigest()
        marker = next((item for item in BLOCK_MARKERS if item in text), None)
        if marker:
            raise FetchFailure(
                f"站点要求人工核验（{marker}），本轮停止该平台",
                url=url,
                requested_at=requested_at,
                elapsed_ms=elapsed,
                http_status=status,
                response_bytes=len(body),
                response_sha256=digest,
                stop_platform=True,
            )
        return NetworkResponse(
            url=url,
            text=text,
            requested_at=requested_at,
            elapsed_ms=elapsed,
            http_status=status,
            response_bytes=len(body),
            response_sha256=digest,
        )


def eastmoney_url(code: str, page: int) -> str:
    query = urllib.parse.urlencode({
        "code": code,
        "sorttype": 1,
        "ps": 100,
        "p": page,
        "from": "CommonBaPost",
        # 固定、非轮换的匿名客户端标识；不用于规避站点限制。
        "deviceid": "00000000-0000-4000-8000-000000000060",
        "version": 200,
        "product": "Guba",
        "plat": "Web",
    })
    return "https://gbapi.eastmoney.com/webarticlelist/api/Article/Articlelist?" + query


def parse_eastmoney_page(
    response: NetworkResponse,
    spec: SourceSpec,
    state: sqlite3.Row,
) -> ParsedPage:
    page = int(state["next_page"] or 1)
    try:
        payload = json.loads(response.text)
    except json.JSONDecodeError as exc:
        raise ValueError("东方财富返回的不是 JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("返回结构异常")
    message = str(payload.get("me") or "").strip()
    rows_value = payload.get("re")
    if message in EASTMONEY_END_MESSAGES and rows_value in (None, []):
        return ParsedPage(
            request_kind="page",
            cursor={"page": page, "pageSize": 100},
            rows_returned=0,
            posts=[],
            all_times=[],
            next_page=page + 1,
            empty_is_complete=True,
        )
    if not isinstance(rows_value, list):
        raise ValueError(message or "返回结构异常")
    if payload.get("me") not in (None, "操作成功"):
        raise ValueError(str(payload.get("me")))
    rows = rows_value
    all_times = [
        published for published in (core.parse_time(row.get("post_publish_time"))
                                    for row in rows if isinstance(row, dict))
        if published is not None
    ]
    posts: List[Dict[str, object]] = []
    user_counts: Counter[str] = Counter()
    for row in rows:
        if not isinstance(row, dict):
            continue
        if core.safe_int(row.get("post_type"), -1) != 0:
            continue
        nickname = str(row.get("user_nickname") or "").strip()
        if not nickname or str(row.get("post_source_id") or "").strip():
            continue
        if any(word in nickname for word in core.AUTHOR_BLOCK_WORDS):
            continue
        user_key = str(row.get("user_id") or nickname)
        if user_counts[user_key] >= 3:
            continue
        text = core.clean_text(row.get("post_title"))
        published = core.parse_time(row.get("post_publish_time"))
        if not core.valid_text(text) or published is None:
            continue
        user_counts[user_key] += 1
        posts.append({
            "post_id": str(row.get("post_id") or core.normalize_text(text)),
            "published": published,
            "title": text,
            "author_hash": author_hash("eastmoney", user_key),
            "comments": max(0, core.safe_int(row.get("post_comment_count"))),
            "clicks": max(0, core.safe_int(row.get("post_click_count"))),
            "likes": 0,
            "kind": "post",
        })
    total_count = core.safe_int(payload.get("count"), 0)
    return ParsedPage(
        request_kind="page",
        cursor={"page": page, "pageSize": 100},
        rows_returned=len(rows),
        posts=posts,
        all_times=all_times,
        next_page=page + 1,
        total_count=total_count if total_count > 0 else None,
    )


def sina_symbol(code: str) -> str:
    return core.sina_symbol(code)


def sina_discovery_url(code: str) -> str:
    return "https://guba.sina.cn/?" + urllib.parse.urlencode({
        "s": "bar",
        "name": sina_symbol(code),
        "from": "redirect",
    })


def parse_sina_discovery(response: NetworkResponse, state: sqlite3.Row) -> ParsedPage:
    patterns = (
        r'["\']bid["\']\s*:\s*["\']?(\d+)',
        r"\bbid\s*[:=]\s*['\"]?(\d+)",
        r"data-bid=['\"](\d+)",
    )
    for pattern in patterns:
        match = re.search(pattern, response.text, re.I)
        if match:
            return ParsedPage(
                request_kind="discover_bid",
                cursor={"symbol": sina_symbol(str(state["code"]))},
                rows_returned=0,
                posts=[],
                all_times=[],
                discovered_external_id=match.group(1),
                empty_is_complete=False,
            )
    raise ValueError("新浪公开页面中未找到论坛编号 bid")


def sina_sample_offset(spec: SourceSpec, spacing_days: int) -> int:
    if spacing_days <= 1:
        return 0
    for sector_key in spec.sectors:
        if sector_key == "__market__" or sector_key not in core.SECTORS:
            continue
        stocks = list(
            core.SECTORS[sector_key].get("sinaStocks")
            or core.SECTORS[sector_key].get("sourceStocks")
            or core.SECTORS[sector_key].get("stocks")
            or []
        )
        for index, stock in enumerate(stocks):
            if str(stock[0]) == spec.code:
                return index % spacing_days
    return int(hashlib.sha256(spec.key.encode("utf-8")).hexdigest()[:8], 16) % spacing_days


def sina_target_dates(
    spec: SourceSpec,
    raw_cutoff: datetime,
    as_of: datetime,
    spacing_days: int,
) -> List[date]:
    spacing = max(1, int(spacing_days))
    offset = sina_sample_offset(spec, spacing)
    current = raw_cutoff.date() + timedelta(days=offset)
    targets: List[date] = []
    while current <= as_of.date():
        targets.append(current)
        current += timedelta(days=spacing)
    return targets


def prioritize_sina_score_window(
    conn: sqlite3.Connection,
    specs: Sequence[SourceSpec],
    *,
    raw_cutoff: datetime,
    score_start: date,
    as_of: datetime,
    spacing_days: int,
) -> Dict[str, object]:
    """Fast-forward unfinished Sina representative sources to the scoring window.

    Representative sampling intentionally starts at ``raw_cutoff`` so a fresh
    archive can retain extra context.  On a bounded/resumable backfill, however,
    spending the first several rotations outside the actual index window delays
    useful cross-platform coverage.  This migration only advances each source's
    target cursor; it never removes posts or request-audit rows, and it records
    every cursor change in metadata for later inspection.
    """

    changes: List[Dict[str, object]] = []
    states = source_state_map(conn, [spec.key for spec in specs])
    for spec in specs:
        if spec.platform != "sina":
            continue
        state = states.get(spec.key)
        if state is None:
            continue
        targets = sina_target_dates(spec, raw_cutoff, as_of, spacing_days)
        first_scoring_index = next(
            (index for index, target in enumerate(targets) if target >= score_start),
            len(targets),
        )
        previous_index = int(state["sample_index"] or 0)
        if previous_index >= first_scoring_index:
            continue
        previous_status = str(state["status"] or "pending")
        next_status = "running" if previous_status == "capped" else previous_status
        conn.execute(
            """
            UPDATE sources
            SET sample_index = ?, status = ?, updated_at = ?
            WHERE source_key = ?
            """,
            (first_scoring_index, next_status, iso_now(), spec.key),
        )
        changes.append(
            {
                "source": spec.key,
                "fromSampleIndex": previous_index,
                "toSampleIndex": first_scoring_index,
                "firstScoringTarget": (
                    targets[first_scoring_index].isoformat()
                    if first_scoring_index < len(targets)
                    else None
                ),
            }
        )

    event = {
        "appliedAt": iso_now(),
        "scoreStart": score_start.isoformat(),
        "rawCutoff": raw_cutoff.date().isoformat(),
        "spacingDays": int(spacing_days),
        "changedSources": len(changes),
        "changes": changes,
        "note": "Only target cursors were advanced; existing posts and request audits were retained.",
    }
    log_key = "sina_score_window_priority_log"
    raw_log = get_meta(conn, log_key)
    try:
        event_log = json.loads(raw_log) if raw_log else []
    except json.JSONDecodeError:
        event_log = []
    if not isinstance(event_log, list):
        event_log = []
    if not event_log:
        previous_event = get_meta(conn, "sina_score_window_priority")
        try:
            previous_payload = json.loads(previous_event) if previous_event else None
        except json.JSONDecodeError:
            previous_payload = None
        if isinstance(previous_payload, dict):
            event_log.append(previous_payload)
    event_log.append(event)
    set_meta(conn, "sina_score_window_priority", json_compact(event))
    set_meta(conn, log_key, json_compact(event_log[-50:]))
    conn.commit()
    return event


def sina_api_url(
    state: sqlite3.Row,
    spec: SourceSpec,
    *,
    strategy: str,
    raw_cutoff: datetime,
    as_of: datetime,
    spacing_days: int,
) -> Tuple[str, Dict[str, object]]:
    query: Dict[str, object] = {
        "s": "h5bar",
        "bid": str(state["external_id"]),
        "num": 50,
    }
    cursor: Dict[str, object]
    if strategy == "representative":
        targets = sina_target_dates(spec, raw_cutoff, as_of, spacing_days)
        index = int(state["sample_index"] or 0)
        if index >= len(targets):
            raise ValueError("新浪代表性日期采样已经完成")
        target = targets[index]
        target_end = datetime.combine(target, datetime_time.max, tzinfo=CN_TZ)
        query["relate_value"] = int(target_end.timestamp())
        query["relate_tid"] = 0
        cursor = {
            "targetDate": target.isoformat(),
            "sampleIndex": index,
            "spacingDays": spacing_days,
            "pageSize": 50,
        }
    elif state["cursor_value"] and state["cursor_tid"]:
        query["relate_value"] = str(state["cursor_value"])
        query["relate_tid"] = str(state["cursor_tid"])
        cursor = {
            "relateValue": str(state["cursor_value"]),
            "relateTid": str(state["cursor_tid"]),
            "pageSize": 50,
        }
    else:
        cursor = {"relateValue": "", "relateTid": "", "pageSize": 50}
    return "https://guba.sina.cn/api/?" + urllib.parse.urlencode(query), cursor


def is_truthy(value: object) -> bool:
    return value is True or str(value or "").strip().lower() in {"1", "true", "yes"}


def parse_sina_page(
    response: NetworkResponse,
    spec: SourceSpec,
    state: sqlite3.Row,
) -> ParsedPage:
    try:
        payload = json.loads(response.text)
    except json.JSONDecodeError as exc:
        raise ValueError("新浪返回的不是 JSON") from exc
    data = payload.get("data") if isinstance(payload, dict) else None
    rows = data.get("threads") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise ValueError("新浪返回结构异常：缺少 data.threads")
    all_times = [
        published for published in (
            core.parse_time(row.get("timestamp") or row.get("lastctime") or row.get("ctime"))
            for row in rows if isinstance(row, dict)
        ) if published is not None
    ]
    posts: List[Dict[str, object]] = []
    user_counts: Counter[str] = Counter()
    for row in rows:
        if not isinstance(row, dict):
            continue
        user = row.get("user") if isinstance(row.get("user"), dict) else {}
        if is_truthy(user.get("verified")):
            continue
        nickname = str(user.get("nick") or user.get("name") or "")
        if any(word in nickname for word in core.AUTHOR_BLOCK_WORDS):
            continue
        user_key = str(user.get("uid") or nickname or row.get("tid") or "unknown")
        if user_counts[user_key] >= 3:
            continue
        text = core.clean_text(row.get("title") or row.get("content") or row.get("long_content"))
        published = core.parse_time(row.get("timestamp") or row.get("lastctime") or row.get("ctime"))
        if not core.valid_text(text) or published is None:
            continue
        user_counts[user_key] += 1
        posts.append({
            "post_id": str(row.get("tid") or core.normalize_text(text)),
            "published": published,
            "title": text,
            "author_hash": author_hash("sina", user_key),
            "comments": max(0, core.safe_int(row.get("reply"))),
            "clicks": max(0, core.safe_int(row.get("views"))),
            "likes": max(0, core.safe_int(row.get("like"))),
            "kind": "post",
        })
    last_row = rows[-1] if rows else {}
    next_value = str(last_row.get("timestamp") or "") if isinstance(last_row, dict) else ""
    next_tid = str(last_row.get("tid") or "") if isinstance(last_row, dict) else ""
    return ParsedPage(
        request_kind="cursor_page",
        cursor={
            "relateValue": str(state["cursor_value"] or ""),
            "relateTid": str(state["cursor_tid"] or ""),
            "pageSize": 50,
        },
        rows_returned=len(rows),
        posts=posts,
        all_times=all_times,
        next_cursor_value=next_value or None,
        next_cursor_tid=next_tid or None,
    )


def page_past_cutoff_streak(
    all_times: Sequence[datetime],
    raw_cutoff: datetime,
    previous: int,
) -> int:
    if not all_times:
        return previous
    center = statistics.median(item.timestamp() for item in all_times)
    return previous + 1 if center <= raw_cutoff.timestamp() else 0


def insert_posts(
    conn: sqlite3.Connection,
    spec: SourceSpec,
    posts: Sequence[Mapping[str, object]],
    raw_cutoff: datetime,
    as_of: datetime,
    observed_at: str,
) -> int:
    inserted = 0
    for post in posts:
        published = post.get("published")
        if not isinstance(published, datetime):
            continue
        if published < raw_cutoff or published > as_of:
            continue
        before = conn.total_changes
        conn.execute(
            """
            INSERT OR IGNORE INTO posts(
                platform, source_key, post_id, source_code, source_name,
                published_at, observed_at, title, author_hash,
                comments, clicks, likes, kind
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                spec.platform,
                spec.key,
                str(post["post_id"]),
                spec.code,
                spec.name,
                published.astimezone(CN_TZ).isoformat(),
                observed_at,
                str(post.get("title") or "")[:260],
                str(post.get("author_hash") or ""),
                int(post.get("comments") or 0),
                int(post.get("clicks") or 0),
                int(post.get("likes") or 0),
                str(post.get("kind") or "post"),
            ),
        )
        if conn.total_changes > before:
            inserted += 1
            for sector_key in spec.sectors:
                conn.execute(
                    "INSERT OR IGNORE INTO post_sectors(source_key, post_id, sector_key) "
                    "VALUES(?, ?, ?)",
                    (spec.key, str(post["post_id"]), sector_key),
                )
    return inserted


def audit_request(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    request_no: int,
    spec: SourceSpec,
    request_kind: str,
    cursor: Mapping[str, object],
    response: Optional[NetworkResponse],
    failure: Optional[FetchFailure],
    result: str,
    rows_returned: int = 0,
    posts_accepted: int = 0,
    posts_inserted: int = 0,
    all_times: Sequence[datetime] = (),
    message: str = "",
) -> None:
    oldest = min(all_times).isoformat() if all_times else None
    newest = max(all_times).isoformat() if all_times else None
    conn.execute(
        """
        INSERT INTO audit(
            run_id, request_no, source_key, platform, request_kind, cursor_json,
            url, requested_at, elapsed_ms, http_status, result,
            rows_returned, posts_accepted, posts_inserted,
            oldest_returned, newest_returned,
            response_bytes, response_sha256, message
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            request_no,
            spec.key,
            spec.platform,
            request_kind,
            json_compact(cursor),
            response.url if response else failure.url if failure else "",
            response.requested_at if response else failure.requested_at if failure else iso_now(),
            response.elapsed_ms if response else failure.elapsed_ms if failure else 0,
            response.http_status if response else failure.http_status if failure else None,
            result,
            rows_returned,
            posts_accepted,
            posts_inserted,
            oldest,
            newest,
            response.response_bytes if response else failure.response_bytes if failure else 0,
            response.response_sha256 if response else failure.response_sha256 if failure else "",
            message[:1000],
        ),
    )


def mark_failure(
    conn: sqlite3.Connection,
    spec: SourceSpec,
    error: str,
    error_cap: int,
) -> str:
    state = conn.execute(
        "SELECT consecutive_errors FROM sources WHERE source_key = ?", (spec.key,)
    ).fetchone()
    errors = int(state[0] or 0) + 1
    status = "blocked" if errors >= error_cap else "error"
    conn.execute(
        """
        UPDATE sources
        SET status = ?, consecutive_errors = ?, last_error = ?,
            total_requests = total_requests + 1, updated_at = ?
        WHERE source_key = ?
        """,
        (status, errors, error[:1000], iso_now(), spec.key),
    )
    return status


def collect_one(
    conn: sqlite3.Connection,
    client: LowFrequencyClient,
    spec: SourceSpec,
    *,
    run_id: str,
    raw_cutoff: datetime,
    as_of: datetime,
    args: argparse.Namespace,
) -> Dict[str, object]:
    state = conn.execute("SELECT * FROM sources WHERE source_key = ?", (spec.key,)).fetchone()
    if state is None:
        raise RuntimeError(f"来源状态缺失：{spec.key}")

    if spec.platform == "eastmoney":
        url = eastmoney_url(spec.code, int(state["next_page"] or 1))
        request_kind = "page"
        cursor = {"page": int(state["next_page"] or 1), "pageSize": 100}
    elif not state["external_id"]:
        url = sina_discovery_url(spec.code)
        request_kind = "discover_bid"
        cursor = {"symbol": sina_symbol(spec.code)}
    else:
        url, representative_cursor = sina_api_url(
            state,
            spec,
            strategy=args.strategy,
            raw_cutoff=raw_cutoff,
            as_of=as_of,
            spacing_days=args.sina_target_spacing,
        )
        request_kind = "cursor_page"
        cursor = representative_cursor

    response: Optional[NetworkResponse] = None
    try:
        response = client.get(url, spec.platform)
        if spec.platform == "eastmoney":
            parsed = parse_eastmoney_page(response, spec, state)
        elif request_kind == "discover_bid":
            parsed = parse_sina_discovery(response, state)
        else:
            parsed = parse_sina_page(response, spec, state)
            if args.strategy == "representative":
                parsed.cursor = dict(cursor)
    except RequestBudgetExhausted:
        raise
    except FetchFailure as failure:
        status = mark_failure(conn, spec, str(failure), args.error_cap)
        audit_request(
            conn,
            run_id=run_id,
            request_no=client.request_count,
            spec=spec,
            request_kind=request_kind,
            cursor=cursor,
            response=None,
            failure=failure,
            result="blocked" if failure.stop_platform else "network_error",
            message=str(failure),
        )
        conn.commit()
        return {
            "ok": False,
            "status": status,
            "error": str(failure),
            "stop_platform": failure.stop_platform,
        }
    except (ValueError, TypeError, KeyError) as exc:
        synthetic = FetchFailure(
            str(exc),
            url=response.url if response else url,
            requested_at=response.requested_at if response else iso_now(),
            elapsed_ms=response.elapsed_ms if response else 0,
            http_status=response.http_status if response else None,
            response_bytes=response.response_bytes if response else 0,
            response_sha256=response.response_sha256 if response else "",
        )
        status = mark_failure(conn, spec, str(exc), args.error_cap)
        audit_request(
            conn,
            run_id=run_id,
            request_no=client.request_count,
            spec=spec,
            request_kind=request_kind,
            cursor=cursor,
            response=response,
            failure=synthetic,
            result="parse_error",
            message=str(exc),
        )
        conn.commit()
        return {"ok": False, "status": status, "error": str(exc), "stop_platform": False}

    assert response is not None
    observed_at = response.requested_at
    inserted = insert_posts(conn, spec, parsed.posts, raw_cutoff, as_of, observed_at)
    previous_streak = int(state["past_cutoff_pages"] or 0)
    if args.strategy == "representative" and spec.platform == "sina":
        streak = previous_streak
    else:
        streak = page_past_cutoff_streak(parsed.all_times, raw_cutoff, previous_streak)
    empty_complete = parsed.empty_is_complete and parsed.rows_returned == 0
    complete = empty_complete or (
        streak >= 2 and not (args.strategy == "representative" and spec.platform == "sina")
    )
    status = "complete" if complete else "running"

    if parsed.discovered_external_id:
        conn.execute(
            """
            UPDATE sources
            SET external_id = ?, status = 'running', consecutive_errors = 0,
                last_error = NULL, total_requests = total_requests + 1,
                updated_at = ?
            WHERE source_key = ?
            """,
            (parsed.discovered_external_id, iso_now(), spec.key),
        )
        status = "running"
    else:
        page_used = int(state["next_page"] or 1)
        if args.strategy == "representative" and spec.platform == "eastmoney":
            next_page = max(2, page_used * 2)
        else:
            next_page = parsed.next_page if parsed.next_page is not None else page_used
        next_cursor_value = (
            parsed.next_cursor_value if parsed.next_cursor_value is not None else state["cursor_value"]
        )
        next_cursor_tid = (
            parsed.next_cursor_tid if parsed.next_cursor_tid is not None else state["cursor_tid"]
        )
        next_sample_index = int(state["sample_index"] or 0)
        if spec.platform == "eastmoney":
            if parsed.total_count and page_used >= math.ceil(parsed.total_count / 100):
                status = "complete"
            elif int(state["total_requests"] or 0) + 1 >= args.eastmoney_page_cap and status != "complete":
                status = "capped"
        elif spec.platform == "sina":
            if args.strategy == "representative":
                targets = sina_target_dates(
                    spec, raw_cutoff, as_of, args.sina_target_spacing
                )
                next_sample_index += 1
                status = "complete" if next_sample_index >= len(targets) else "running"
            else:
                same_cursor = (
                    parsed.next_cursor_value == str(state["cursor_value"] or "")
                    and parsed.next_cursor_tid == str(state["cursor_tid"] or "")
                )
                if same_cursor and parsed.rows_returned:
                    status = "capped"
            if int(state["total_requests"] or 0) + 1 >= args.sina_page_cap and status != "complete":
                status = "capped"

        oldest = min(parsed.all_times).isoformat() if parsed.all_times else state["oldest_seen"]
        newest = max(parsed.all_times).isoformat() if parsed.all_times else state["newest_seen"]
        if state["oldest_seen"] and oldest:
            oldest = min(str(state["oldest_seen"]), oldest)
        if state["newest_seen"] and newest:
            newest = max(str(state["newest_seen"]), newest)
        conn.execute(
            """
            UPDATE sources
            SET status = ?, next_page = ?, cursor_value = ?, cursor_tid = ?, sample_index = ?,
                past_cutoff_pages = ?, total_requests = total_requests + 1,
                rows_seen = rows_seen + ?, posts_saved = posts_saved + ?,
                consecutive_errors = 0, last_error = NULL,
                oldest_seen = ?, newest_seen = ?, updated_at = ?
            WHERE source_key = ?
            """,
            (
                status,
                next_page,
                next_cursor_value,
                next_cursor_tid,
                next_sample_index,
                streak,
                parsed.rows_returned,
                inserted,
                oldest,
                newest,
                iso_now(),
                spec.key,
            ),
        )

    audit_request(
        conn,
        run_id=run_id,
        request_no=client.request_count,
        spec=spec,
        request_kind=parsed.request_kind,
        cursor=parsed.cursor,
        response=response,
        failure=None,
        result=status,
        rows_returned=parsed.rows_returned,
        posts_accepted=len(parsed.posts),
        posts_inserted=inserted,
        all_times=parsed.all_times,
        message=(
            f"bid={parsed.discovered_external_id}" if parsed.discovered_external_id
            else f"pastCutoffStreak={streak}"
        ),
    )
    conn.commit()
    return {
        "ok": True,
        "status": status,
        "rows": parsed.rows_returned,
        "accepted": len(parsed.posts),
        "inserted": inserted,
        "oldest": min(parsed.all_times).isoformat() if parsed.all_times else None,
        "newest": max(parsed.all_times).isoformat() if parsed.all_times else None,
        "request_kind": parsed.request_kind,
    }


def source_state_map(conn: sqlite3.Connection, keys: Sequence[str]) -> Dict[str, sqlite3.Row]:
    if not keys:
        return {}
    placeholders = ",".join("?" for _ in keys)
    rows = conn.execute(
        f"SELECT * FROM sources WHERE source_key IN ({placeholders})", keys
    ).fetchall()
    return {str(row["source_key"]): row for row in rows}


def generate_report(
    conn: sqlite3.Connection,
    *,
    report_path: Optional[Path],
    sector_names: Mapping[str, str],
) -> Dict[str, object]:
    as_of = get_meta(conn, "window_as_of")
    raw_cutoff = get_meta(conn, "raw_cutoff")
    score_start = get_meta(conn, "score_start")
    source_rows = conn.execute(
        "SELECT platform, status, COUNT(*) AS n, SUM(total_requests) AS requests, "
        "SUM(rows_seen) AS rows_seen, SUM(posts_saved) AS posts_saved "
        "FROM sources GROUP BY platform, status ORDER BY platform, status"
    ).fetchall()
    source_summary: Dict[str, Dict[str, object]] = defaultdict(dict)
    for row in source_rows:
        source_summary[str(row["platform"])][str(row["status"])] = {
            "sources": int(row["n"] or 0),
            "requests": int(row["requests"] or 0),
            "rowsSeen": int(row["rows_seen"] or 0),
            "postsSaved": int(row["posts_saved"] or 0),
        }

    per_source_rows = conn.execute(
        """
        SELECT s.*,
               COALESCE(a.requests, 0) AS audited_requests,
               COALESCE(a.successes, 0) AS successful_requests,
               COALESCE(a.failures, 0) AS failed_requests,
               COALESCE(a.rows_returned, 0) AS audited_rows_returned,
               COALESCE(a.posts_accepted, 0) AS audited_posts_accepted,
               COALESCE(a.posts_inserted, 0) AS audited_posts_inserted
        FROM sources s
        LEFT JOIN (
            SELECT source_key,
                   COUNT(*) AS requests,
                   SUM(CASE WHEN result NOT IN ('network_error','parse_error','blocked')
                            THEN 1 ELSE 0 END) AS successes,
                   SUM(CASE WHEN result IN ('network_error','parse_error','blocked')
                            THEN 1 ELSE 0 END) AS failures,
                   SUM(rows_returned) AS rows_returned,
                   SUM(posts_accepted) AS posts_accepted,
                   SUM(posts_inserted) AS posts_inserted
            FROM audit
            GROUP BY source_key
        ) a ON a.source_key = s.source_key
        ORDER BY s.platform, s.source_key
        """
    ).fetchall()
    per_source = []
    for row in per_source_rows:
        returned = int(row["audited_rows_returned"] or 0)
        accepted = int(row["audited_posts_accepted"] or 0)
        requests = int(row["audited_requests"] or 0)
        successes = int(row["successful_requests"] or 0)
        per_source.append({
            "key": row["source_key"],
            "platform": row["platform"],
            "code": row["code"],
            "name": row["name"],
            "sectors": json.loads(row["sectors_json"] or "[]"),
            "status": row["status"],
            "nextPage": int(row["next_page"] or 1) if row["platform"] == "eastmoney" else None,
            "nextCursor": {
                "relateValue": row["cursor_value"],
                "relateTid": row["cursor_tid"],
            } if row["platform"] == "sina" else None,
            "representativeSampleIndex": int(row["sample_index"] or 0),
            "oldestReturned": row["oldest_seen"],
            "newestReturned": row["newest_seen"],
            "requests": requests,
            "successfulRequests": successes,
            "failedRequests": int(row["failed_requests"] or 0),
            "requestSuccessRate": round(successes / requests, 4) if requests else None,
            "rowsReturned": returned,
            "postsAcceptedByOrdinaryUserFilter": accepted,
            "acceptedRowRate": round(accepted / returned, 4) if returned else None,
            "newPostsSavedInWindow": int(row["audited_posts_inserted"] or 0),
            "pastCutoffPages": int(row["past_cutoff_pages"] or 0),
            "consecutiveErrors": int(row["consecutive_errors"] or 0),
            "lastError": row["last_error"],
            "updatedAt": row["updated_at"],
            "rateDefinition": (
                "acceptedRowRate 仅表示已抓取页面中通过普通用户过滤的比例；"
                "不是论坛全量采样率"
            ),
        })

    post_rows = conn.execute(
        "SELECT platform, COUNT(*) AS n, MIN(published_at) AS oldest, "
        "MAX(published_at) AS newest FROM posts GROUP BY platform"
    ).fetchall()
    posts_by_platform = {
        str(row["platform"]): {
            "posts": int(row["n"] or 0),
            "oldest": row["oldest"],
            "newest": row["newest"],
        }
        for row in post_rows
    }

    daily_rows = conn.execute(
        """
        SELECT substr(published_at, 1, 10) AS day,
               COUNT(*) AS posts,
               SUM(CASE WHEN platform = 'eastmoney' THEN 1 ELSE 0 END) AS eastmoney,
               SUM(CASE WHEN platform = 'sina' THEN 1 ELSE 0 END) AS sina
        FROM posts
        WHERE substr(published_at, 1, 10) >= ?
        GROUP BY substr(published_at, 1, 10)
        ORDER BY day
        """,
        (score_start or "",),
    ).fetchall()
    daily = [
        {
            "date": row["day"],
            "posts": int(row["posts"] or 0),
            "eastmoney": int(row["eastmoney"] or 0),
            "sina": int(row["sina"] or 0),
        }
        for row in daily_rows
    ]

    sector_rows = conn.execute(
        """
        SELECT ps.sector_key,
               COUNT(*) AS posts,
               COUNT(DISTINCT substr(p.published_at, 1, 10)) AS days_covered,
               MIN(p.published_at) AS oldest,
               MAX(p.published_at) AS newest,
               COUNT(DISTINCT p.source_key) AS sources,
               GROUP_CONCAT(DISTINCT p.platform) AS platforms
        FROM post_sectors ps
        JOIN posts p ON p.source_key = ps.source_key AND p.post_id = ps.post_id
        WHERE substr(p.published_at, 1, 10) >= ?
        GROUP BY ps.sector_key
        ORDER BY ps.sector_key
        """,
        (score_start or "",),
    ).fetchall()
    sector_coverage = [
        {
            "key": row["sector_key"],
            "name": sector_names.get(str(row["sector_key"]), str(row["sector_key"])),
            "posts": int(row["posts"] or 0),
            "daysCovered": int(row["days_covered"] or 0),
            "sourcesWithPosts": int(row["sources"] or 0),
            "platforms": sorted(str(row["platforms"] or "").split(",")) if row["platforms"] else [],
            "oldest": row["oldest"],
            "newest": row["newest"],
        }
        for row in sector_rows
    ]

    run_row = conn.execute(
        "SELECT COUNT(*) AS runs, SUM(requests) AS requests, SUM(errors) AS errors "
        "FROM runs"
    ).fetchone()
    audit_row = conn.execute(
        "SELECT COUNT(*) AS requests, SUM(CASE WHEN result IN "
        "('network_error','parse_error','blocked') THEN 1 ELSE 0 END) AS errors "
        "FROM audit"
    ).fetchone()
    report = {
        "schemaVersion": SCHEMA_VERSION,
        "collectorVersion": COLLECTOR_VERSION,
        "generatedAt": iso_now(),
        "window": {
            "asOf": as_of,
            "scoreStart": score_start,
            "rawCutoff": raw_cutoff,
            "scoreDays": int(get_meta(conn, "score_days") or 60),
            "contextDays": int(get_meta(conn, "context_days") or 30),
        },
        "collectionPolicy": {
            "strategy": get_meta(conn, "collection_strategy"),
            "sinaTargetSpacingDays": int(
                get_meta(conn, "sina_target_spacing_days") or 2
            ),
            "sourceRotation": "每轮每来源最多一次请求，优先完成跨板块覆盖",
        },
        "sources": dict(source_summary),
        "perSource": per_source,
        "postsByPlatform": posts_by_platform,
        "dailyScoreWindowCoverage": daily,
        "sectorScoreWindowCoverage": sector_coverage,
        "audit": {
            "runs": int(run_row["runs"] or 0),
            "runRequests": int(run_row["requests"] or 0),
            "runErrors": int(run_row["errors"] or 0),
            "auditedRequests": int(audit_row["requests"] or 0),
            "auditedErrors": int(audit_row["errors"] or 0),
        },
        "estimatedScoringPolicy": {
            "defaultView": "estimated_posts",
            "ignoreCommentsClicksLikes": True,
            "reason": "互动数是回溯采集时的观察值，若用于历史发布日期会产生前视偏差",
            "rawInteractionFieldsRetained": [
                "observed_comments",
                "observed_clicks",
                "observed_likes",
                "collected_at",
            ],
        },
        "limitations": [
            "这些是回溯时仍可见的公开帖子，不等于当日真实运行时的完整帖子集合。",
            "互动数是采集时观察值，不能作为历史发布日期当天的互动数。",
            "帖子可能被删除、置顶或重新排序；不同来源的历史深度不一致。",
            "同花顺因不能可靠按日期回溯，未纳入本采集器。",
            "公开可访问不代表平台授权批量再利用，运行者需自行确认使用权限。",
        ],
    }
    if report_path is not None:
        atomic_write_json(report_path, report)
    return report


def print_plan(specs: Sequence[SourceSpec], args: argparse.Namespace) -> None:
    counts = Counter(spec.platform for spec in specs)
    sector_counts: Counter[str] = Counter()
    for spec in specs:
        sector_counts.update(spec.sectors)
    print("历史采集计划（未联网、未写文件）")
    print(f"目标来源：{len(specs)} 个；东方财富 {counts['eastmoney']}，新浪 {counts['sina']}")
    print(f"指数窗口：{args.days} 日；额外上下文：{args.context_days} 日")
    print(f"本轮请求上限：{args.max_requests}；请求间隔：{max(1.0, args.delay):.1f} 秒")
    print(
        f"每来源硬请求上限：东方财富 {args.eastmoney_page_cap} 页，"
        f"新浪 {args.sina_page_cap} 次（含缺失 bid 的一次发现请求）"
    )
    as_of = parse_as_of(args.as_of)
    score_start = as_of.date() - timedelta(days=args.days - 1)
    raw_cutoff = datetime.combine(
        score_start - timedelta(days=args.context_days),
        datetime_time.min,
        tzinfo=CN_TZ,
    )
    east_count = counts["eastmoney"]
    sina_specs = [spec for spec in specs if spec.platform == "sina"]
    cached_bids = read_json(SINA_BID_CACHE_PATH, {})
    if not isinstance(cached_bids, dict):
        cached_bids = {}
    if args.strategy == "representative":
        sina_pages = sum(len(sina_target_dates(
            spec, raw_cutoff, as_of, args.sina_target_spacing
        )) for spec in sina_specs)
        bid_requests = sum(spec.code not in cached_bids for spec in sina_specs)
        estimated_cap = east_count * args.eastmoney_page_cap + sina_pages + bid_requests
        print(
            f"代表性策略：新浪两只代表股按 {args.sina_target_spacing} 日错峰，"
            f"计划日期页约 {sina_pages}，bid 发现最多 {bid_requests}。"
        )
        print(
            f"按每来源硬上限估算总请求不超过约 {estimated_cap} 次；"
            f"纯限频等待下限约 {estimated_cap * max(1.0, args.delay) / 60:.1f} 分钟，"
            "实际还需叠加站点响应时间。"
        )
    else:
        estimated_cap = (
            east_count * args.eastmoney_page_cap
            + len(sina_specs) * args.sina_page_cap
        )
        print(f"连续翻页策略的硬上限约 {estimated_cap} 次请求。")
    print("板块来源覆盖：")
    for key in ["__market__", *core.SECTORS.keys()]:
        if sector_counts[key]:
            label = "全市场" if key == "__market__" else str(core.SECTORS[key]["name"])
            print(f"  {label}: {sector_counts[key]} 个来源")
    print("注意：完整回溯的实际请求数由各论坛活跃度决定，不能预先保证。")


def create_run(conn: sqlite3.Connection, args: argparse.Namespace) -> str:
    run_id = str(uuid.uuid4())
    args_payload = {
        key: str(value) if isinstance(value, Path) else value
        for key, value in vars(args).items()
    }
    conn.execute(
        "INSERT INTO runs(run_id, started_at, status, args_json) VALUES(?, ?, 'running', ?)",
        (run_id, iso_now(), json_compact(args_payload)),
    )
    conn.commit()
    return run_id


def finish_run(
    conn: sqlite3.Connection,
    run_id: str,
    *,
    status: str,
    requests: int,
    rows_seen: int,
    posts_inserted: int,
    errors: int,
    note: str = "",
) -> None:
    conn.execute(
        """
        UPDATE runs
        SET finished_at = ?, status = ?, requests = ?, rows_seen = ?,
            posts_inserted = ?, errors = ?, note = ?
        WHERE run_id = ?
        """,
        (iso_now(), status, requests, rows_seen, posts_inserted, errors, note[:1000], run_id),
    )
    conn.commit()


def collect(
    conn: sqlite3.Connection,
    specs: Sequence[SourceSpec],
    sector_names: Mapping[str, str],
    args: argparse.Namespace,
) -> int:
    as_of, raw_cutoff, score_start = resolve_window(conn, args)
    initialize_sources(conn, specs, args.retry_blocked)
    if args.sina_prioritize_score_window and args.strategy == "representative":
        priority_event = prioritize_sina_score_window(
            conn,
            specs,
            raw_cutoff=raw_cutoff,
            score_start=score_start,
            as_of=as_of,
            spacing_days=args.sina_target_spacing,
        )
        changed_sources = int(priority_event.get("changedSources") or 0)
        print(
            f"Sina scoring-window priority enabled; advanced {changed_sources} source cursors."
        )
    run_id = create_run(conn, args)
    client = LowFrequencyClient(
        max_requests=args.max_requests,
        delay=args.delay,
        timeout=args.timeout,
    )
    blocked_platforms: Set[str] = set()
    rows_seen = 0
    posts_inserted = 0
    errors = 0
    cycles = 0
    interrupted = False
    budget_exhausted = False
    print(
        f"回溯窗口：指数 {score_start.isoformat()} 至 {as_of.date().isoformat()}；"
        f"原帖上下文从 {raw_cutoff.date().isoformat()} 开始。"
    )
    print(
        f"本轮最多 {args.max_requests} 次请求，单线程，间隔至少 "
        f"{max(1.0, args.delay):.1f} 秒。"
    )
    try:
        while client.request_count < args.max_requests:
            cycles += 1
            if args.max_cycles and cycles > args.max_cycles:
                break
            states = source_state_map(conn, [spec.key for spec in specs])
            candidates = [
                spec for spec in specs
                if spec.platform not in blocked_platforms
                and states.get(spec.key) is not None
                and str(states[spec.key]["status"]) in {"pending", "running", "error"}
            ]
            if not candidates:
                break
            attempted = 0
            for spec in candidates:
                if client.request_count >= args.max_requests:
                    budget_exhausted = True
                    break
                attempted += 1
                try:
                    outcome = collect_one(
                        conn,
                        client,
                        spec,
                        run_id=run_id,
                        raw_cutoff=raw_cutoff,
                        as_of=as_of,
                        args=args,
                    )
                except RequestBudgetExhausted:
                    budget_exhausted = True
                    break
                if outcome.get("ok"):
                    rows_seen += int(outcome.get("rows") or 0)
                    posts_inserted += int(outcome.get("inserted") or 0)
                    oldest = str(outcome.get("oldest") or "-")[:10]
                    newest = str(outcome.get("newest") or "-")[:10]
                    print(
                        f"[{client.request_count}/{args.max_requests}] "
                        f"{PLATFORM_NAMES[spec.platform]} · {spec.name}："
                        f"返回 {outcome.get('rows', 0)}，新增 {outcome.get('inserted', 0)}，"
                        f"日期 {oldest}~{newest}，状态 {outcome.get('status')}"
                    )
                else:
                    errors += 1
                    print(
                        f"[{client.request_count}/{args.max_requests}] "
                        f"{PLATFORM_NAMES[spec.platform]} · {spec.name}："
                        f"{outcome.get('error')}（状态 {outcome.get('status')}）"
                    )
                    if outcome.get("stop_platform"):
                        blocked_platforms.add(spec.platform)
                        print(f"本轮停止 {PLATFORM_NAMES[spec.platform]} 的后续请求。")
            if attempted == 0 or budget_exhausted:
                break
    except KeyboardInterrupt:
        interrupted = True
        print("\n收到中断信号；已提交到最近一次请求，可直接再次运行继续。")
    except Exception as exc:
        finish_run(
            conn,
            run_id,
            status="failed",
            requests=client.request_count,
            rows_seen=rows_seen,
            posts_inserted=posts_inserted,
            errors=errors + 1,
            note=str(exc),
        )
        raise

    status = "interrupted" if interrupted else "completed"
    note = "request budget exhausted" if budget_exhausted else ""
    finish_run(
        conn,
        run_id,
        status=status,
        requests=client.request_count,
        rows_seen=rows_seen,
        posts_inserted=posts_inserted,
        errors=errors,
        note=note,
    )
    report_path = None if args.no_report or args.db == ":memory:" else Path(args.report).resolve()
    report = generate_report(conn, report_path=report_path, sector_names=sector_names)
    states = Counter(
        str(row["status"]) for row in conn.execute(
            "SELECT status FROM sources WHERE source_key IN ({})".format(
                ",".join("?" for _ in specs)
            ),
            [spec.key for spec in specs],
        ).fetchall()
    ) if specs else Counter()
    total_posts = sum(int(item.get("posts") or 0) for item in report["postsByPlatform"].values())
    print(
        f"本轮结束：请求 {client.request_count}，读取行 {rows_seen}，新增帖子 {posts_inserted}，"
        f"数据库共 {total_posts} 条；来源状态 {dict(states)}。"
    )
    if report_path:
        print(f"覆盖报告：{report_path}")
    return 130 if interrupted else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="低频、可断点续跑的东方财富+新浪 60 日社区历史采集器",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="SQLite 数据库路径或 :memory:")
    parser.add_argument("--report", default=str(DEFAULT_REPORT_PATH), help="覆盖报告 JSON 路径")
    parser.add_argument("--days", type=int, default=60, help="基础采集自然日跨度；另加档案余量，最终指数按交易日输出")
    parser.add_argument("--context-days", type=int, default=30, help="最早指数点之前额外采集的滚动上下文")
    parser.add_argument("--as-of", help="固定回溯截止时间；日期格式会按当天 23:59:59 处理")
    parser.add_argument(
        "--strategy",
        choices=("representative", "sequential"),
        default="representative",
        help="代表性日期采样或连续全量翻页",
    )
    parser.add_argument(
        "--sina-target-spacing",
        type=int,
        default=2,
        help="代表性模式下每只新浪代表股的目标日期间隔；两只股会错峰",
    )
    parser.add_argument(
        "--sina-prioritize-score-window",
        action="store_true",
        help=(
            "Representative mode only: retain collected context but fast-forward "
            "unfinished Sina target cursors to the first date in the scoring window"
        ),
    )
    parser.add_argument("--platforms", action="append", help="平台列表；默认 eastmoney,sina")
    parser.add_argument(
        "--sina-stocks-per-sector",
        type=int,
        choices=(1, 2),
        default=2,
        help="每板块采集几只已配置代表股的新浪股吧",
    )
    parser.add_argument("--sources", action="append", help="限定来源 key，可逗号分隔或重复传入")
    parser.add_argument("--sectors", action="append", help="限定板块 key，可逗号分隔或重复传入")
    parser.add_argument("--max-requests", type=int, default=20, help="本轮所有网络请求的总上限")
    parser.add_argument("--delay", type=float, default=1.5, help="请求最小间隔；低于 1 秒会自动提升到 1 秒")
    parser.add_argument("--timeout", type=int, default=18, help="单次请求超时秒数")
    parser.add_argument("--max-cycles", type=int, default=0, help="最多轮询来源轮数；0 表示只受请求预算限制")
    parser.add_argument("--eastmoney-page-cap", type=int, default=14, help="东方财富单来源硬页面请求数")
    parser.add_argument("--sina-page-cap", type=int, default=50, help="新浪单来源硬请求数")
    parser.add_argument("--error-cap", type=int, default=3, help="来源连续失败多少轮后标记 blocked")
    parser.add_argument("--retry-blocked", action="store_true", help="重新启用此前 blocked 的来源")
    parser.add_argument("--dry-run", "--plan-only", action="store_true", help="只打印计划，不联网、不写文件")
    parser.add_argument("--report-only", action="store_true", help="只重建覆盖报告，不联网")
    parser.add_argument("--no-report", action="store_true", help="运行结束后不写报告文件")
    parser.add_argument("--probe", action="store_true", help="对东方财富和新浪各做一次小样本请求，不落盘")
    return parser


def validate_args(args: argparse.Namespace) -> None:
    if args.days < 1 or args.days > 366:
        raise ValueError("--days 必须在 1 到 366 之间")
    if args.context_days < 0 or args.context_days > 180:
        raise ValueError("--context-days 必须在 0 到 180 之间")
    if args.max_requests < 1:
        raise ValueError("--max-requests 必须大于 0")
    if args.timeout < 3 or args.timeout > 120:
        raise ValueError("--timeout 必须在 3 到 120 秒之间")
    if args.eastmoney_page_cap < 1 or args.sina_page_cap < 1:
        raise ValueError("页数上限必须大于 0")
    if args.sina_target_spacing < 1 or args.sina_target_spacing > 30:
        raise ValueError("--sina-target-spacing 必须在 1 到 30 天之间")
    if args.error_cap < 1 or args.error_cap > 20:
        raise ValueError("--error-cap 必须在 1 到 20 之间")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.probe:
            args.db = ":memory:"
            args.no_report = True
            args.sources = ["eastmoney:300059,sina:300059"]
            args.platforms = ["eastmoney,sina"]
            args.max_requests = 2
            args.max_cycles = 1
        validate_args(args)
        all_specs, sector_names = build_sources(args.sina_stocks_per_sector)
        specs = filter_sources(all_specs, args)
        if not specs:
            raise ValueError("筛选后没有可采集来源")
        if args.dry_run:
            print_plan(specs, args)
            return 0
        if args.report_only:
            if args.db == ":memory:" or not Path(args.db).exists():
                raise ValueError("--report-only 需要已经存在的数据库文件")
            conn = connect_db(args.db)
            try:
                init_schema(conn)
                report_path = None if args.no_report else Path(args.report).resolve()
                report = generate_report(conn, report_path=report_path, sector_names=sector_names)
                total_posts = sum(
                    int(item.get("posts") or 0) for item in report["postsByPlatform"].values()
                )
                print(f"已重建覆盖报告；数据库帖子 {total_posts} 条。")
                if report_path:
                    print(report_path)
                return 0
            finally:
                conn.close()

        print("仅采集匿名公开内容；遇登录、验证码、403 或 429 会停，不做任何绕过。")
        conn = connect_db(args.db)
        try:
            init_schema(conn)
            return collect(conn, specs, sector_names, args)
        finally:
            conn.close()
    except (ValueError, RuntimeError, sqlite3.Error) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
