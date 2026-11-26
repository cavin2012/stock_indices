"""宝妈·小白·散户社区情绪指数 v6。

只读取公开社区中的普通用户文字与公开互动计数，不使用股价、成交量、
涨跌家数、行情热榜或 RQ 数据。Python 3 标准库即可运行。
"""

from __future__ import annotations

import html
import hashlib
import json
import math
import os
import re
import statistics
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Mapping, Sequence, Tuple

from community_archive import archive_results, load_archived_results
from channel_fairness_model import (
    CHANNELS as FAIR_CHANNELS,
    MODEL_VERSION as FAIRNESS_MODEL_VERSION,
    aggregate_channel_fairness,
)
from history_engine import (
    ESTIMATED,
    MEASURED,
    build_history_meta,
    build_history_point,
    normalized_weighted_average,
    record_type_of,
    upsert_history,
)
from sector_catalog import (
    PARENT_SECTORS,
    SECTOR_CATALOG_ID,
    SECTOR_CATALOG_VERSION,
    SECTORS,
)
from sector_heat import calibrate_sector_heats, stabilize_sector_heats
from trading_calendar import (
    calendar_mode,
    is_trading_day,
    latest_trading_day,
    previous_trading_days,
)


ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
LATEST_PATH = DATA_DIR / "latest.json"
HISTORY_PATH = DATA_DIR / "history.json"
HISTORY_META_PATH = DATA_DIR / "history_meta.json"
V3_HISTORY_PATH = DATA_DIR / "history_community_v3.json"
LEGACY_HISTORY_PATH = DATA_DIR / "history_market_v2.json"
DATA_JS_PATH = ROOT / "data.js"
LOG_PATH = DATA_DIR / "update.log"
FORUM_CACHE_PATH = DATA_DIR / "forum_cache.json"
COMMUNITY_ARCHIVE_DIR = DATA_DIR / "community_archive"
SINA_BID_CACHE_PATH = DATA_DIR / "sina_bid_cache.json"
THS_CONFIG_CACHE_PATH = DATA_DIR / "ths_forum_cache.json"
CN_TZ = timezone(timedelta(hours=8))
NOW = datetime.now(CN_TZ)
VERSION = 6
LOOKBACK_DAYS = 30
TRADING_DAY_WEIGHTS: Tuple[float, float, float] = (0.50, 0.30, 0.20)
TRADING_WINDOW_SIZE = len(TRADING_DAY_WEIGHTS)
ACTIVITY_VELOCITY_WEIGHT = 0.85
ACTIVITY_BREADTH_WEIGHT = 1.0 - ACTIVITY_VELOCITY_WEIGHT
AUTHOR_DAILY_LIMIT = 3
ESTIMATED_SOURCE_DAY_LIMIT = 8
COLLECTOR_PAGE_LIMIT = 3
MARKET_PAGE_LIMIT = 30
MARKET_TIME_BUDGET_SECONDS = 45.0
MARKET_PAGE_INTERVAL_SECONDS = 0.25
MARKET_WINDOW_POLICY_VERSION = 1
EASTMONEY_PAGE_SIZE = 100
SINA_PAGE_SIZE = 50
TAOGUBA_PAGE_SIZE = 20
THS_CURSOR_PAGE_LIMIT = 1
THS_API_BASE: str | None = None


PLATFORM_NAMES = {
    "eastmoney": "东方财富股吧",
    "sina": "新浪股吧",
    "ths": "同花顺板块社区",
    "taoguba": "淘股吧",
}

MARKET_EASTMONEY_SOURCES: List[Tuple[str, str]] = [
    ("zssh000001", "上证指数吧"),
    ("zssz399001", "深证成指吧"),
    ("zssz399006", "创业板指吧"),
]

# Explicit exchange-qualified symbols avoid confusing SH000001 with SZ000001.
MARKET_SINA_SOURCES: List[Tuple[str, str]] = [
    ("sh000001", "上证指数"),
    ("sz399001", "深证成指"),
    ("sz399006", "创业板指"),
]

POSITIVE = {
    "跌不动": 2, "利空出尽": 3, "黄金坑": 3, "涨停": 3, "封板": 3,
    "起飞": 3, "翻倍": 3, "暴涨": 3, "大涨": 3, "普涨": 2,
    "补涨": 2, "牛市": 3, "主升": 3, "新高": 3, "突破": 3,
    "反转": 2, "反包": 3, "大阳线": 2, "走强": 2, "回暖": 1,
    "连板": 3, "大肉": 3, "吃肉": 3, "抄底": 3, "加仓": 3,
    "满仓": 3, "梭哈": 3, "上车": 3, "买入": 3, "看多": 3,
    "利好": 3, "低吸": 3, "龙头": 3, "必涨": 3, "赚钱": 3,
    "发财": 3, "洗盘": 2, "看好": 1, "上涨": 1, "反弹": 1,
    "机会": 1, "强势": 1, "拿住": 1, "格局": 1, "回本": 1,
    "红盘": 1, "稳了": 1, "继续涨": 1, "牛股": 2,
}

NEGATIVE = {
    "看不出来资金在跑路": -3, "看不出资金在跑路": -3,
    "资金正在跑路": -3, "资金在跑路": -3,
    "不要盲目加仓": -2, "别盲目加仓": -2, "卖出当前标的": -3,
    "卖出当前股票": -3, "卖出这只股票": -3, "突破不了": -2,
    "没有龙头": -2, "放量下杀": -3, "放量下跌": -3,
    "资金跑路": -3, "不要加仓": -2, "盲目加仓": -2,
    "不建议追涨": -2, "不要追高": -2, "不能追高": -2, "谨慎追高": -1,
    "避免追高": -2, "关灯吃面": -3, "杀猪盘": -3, "跌停": -3,
    "暴跌": -3, "崩盘": -3, "清仓": -3, "割肉": -3, "爆仓": -3,
    "退市": -3, "完蛋": -3, "血亏": -3, "大面": -3, "跑路": -3,
    "诈骗": -3, "套死": -3, "腰斩": -3, "大跌": -2, "下跌": -2,
    "看空": -2, "利空": -2, "逃命": -2, "砸盘": -2, "出货": -2,
    "套牢": -2, "被套": -2, "亏损": -2, "亏死": -2, "垃圾": -2,
    "骗子": -2, "减仓": -2, "撤退": -2, "风险": -2, "破位": -2,
    "跳水": -2, "阴跌": -2, "熊市": -2, "不涨": -1, "不行": -1,
    "弱势": -1, "难受": -1, "担心": -1, "瑟瑟": -1, "寻底": -1,
    "走下坡": -1, "天天跌": -2, "只会跌": -2, "一直跌": -2,
    "下杀": -2, "跌破": -2, "止损": -2, "卖出": -2,
}

FOMO_WORDS = (
    "梭哈", "满仓", "上车", "追涨", "追高", "踏空", "涨停", "翻倍",
    "起飞", "冲进去", "冲", "抢筹", "加仓", "最后机会", "来不及",
    "错过", "龙头", "必涨", "抄底", "低吸", "拿住", "格局", "抢跑",
)
PANIC_WORDS = (
    "资金在跑路", "资金跑路",
    "割肉", "清仓", "跌停", "崩", "完了", "完蛋", "跑路", "逃命",
    "套死", "爆仓", "救命", "血亏", "腰斩", "关灯吃面", "大跌",
    "暴跌", "亏死", "退市", "瀑布", "踩踏",
)
NEWCOMER_WORDS = (
    "小白", "新手", "新人", "求问", "请教", "能买吗", "还能买吗",
    "怎么走", "怎么办", "怎么操作", "如何", "开户", "不懂", "求带",
    "求解", "什么是", "懂的说说", "老师", "第一次", "哪位知道",
    "能不能买", "可以买吗", "目标价", "成本价", "解套", "仓位多少",
    "现在进", "能上车吗", "该卖吗", "该买吗",
)
QUESTION_WORDS = (
    "买", "卖", "仓", "成本", "解套", "进场", "上车", "目标", "走势",
    "操作", "持有", "补仓", "止损", "怎么办", "怎么走",
)
INTENSE_WORDS = (
    "绝对", "肯定", "必须", "完蛋", "必涨", "必跌", "疯了", "天哪",
    "卧槽", "梭哈", "暴涨", "暴跌", "涨停", "跌停", "翻倍", "崩盘",
    "救命", "全线", "普涨", "大涨", "大跌", "爆发",
)
SPAM_WORDS = (
    "加微信", "加薇", "进群", "加群", "电话", "领取资料", "老师带",
    "带你操作", "免费领取", "开户链接", "彩票", "广告合作", "私信领取",
    "扫码", "公众号", "扣666", "扣888", "扣999", "课程报名",
)
MARKETING_WORDS = (
    "点赞关注", "点赞", "关注我", "关注", "盘中操作", "今日上车",
    "带大家", "挑战翻倍", "粉丝", "直播", "花姐", "老师", "私信",
)
MARKETING_CALLS = ("点赞关注", "关注我", "盘中操作", "今日上车", "带大家", "私信", "评论区")
SKEPTICAL_MARKERS = ("就是个名", "徒有虚名", "名不副实", "所谓", "呵呵", "就这")
UNCERTAINTY_MARKERS = ("看不懂", "不明白", "怎么理解", "什么意思", "说不清")
AUTHOR_BLOCK_WORDS = (
    "官方", "基金", "ETF", "证券", "投顾", "首席", "研究", "研报",
    "财经", "情报", "机构", "数据宝", "交易所", "智库", "媒体",
    "荐股", "带单", "财富号",
)
LOW_INFORMATION = {
    "转发", "签到", "打卡", "路过", "哈哈", "呵呵", "早上好", "早安",
    "晚安", "谢谢", "收到", "支持", "牛", "赞", "666", "888",
}
NEGATION_WORDS = ("不", "没", "没有", "别", "莫", "勿", "避免", "不能", "不要", "谨慎")
NEGATION_SUFFIXES = ("不了", "不成", "失败", "无望")
CAUTION_PREFIXES = ("盲目", "冲动", "随便")

# 三类指数目前仍使用同一市场语料的三套透明权重。以下规则只生成互斥的
# “行为语料证据”和遮蔽证据词后的候选方向分，未通过人工标注验证前不会
# 替换正式宝妈/小白/散户指数。宝妈在这里仅指家庭稳健型表达，不推断身份。
HOUSEHOLD_EXPLICIT_RE = re.compile(
    r"宝妈|宝宝|孩子|小孩|奶粉|学费|家庭|家里|生活费|买菜|房贷|车贷|"
    r"老公|老婆|母婴|养老|退休|工资|存款|闲钱|攒钱"
)
HOUSEHOLD_CAUTIOUS_RE = re.compile(
    r"稳健|长期持有|长线|分红|股息|定投|价值投资|拿着不动|不折腾|"
    r"低风险|保本|每个月|每月投入|闲钱投资|养老钱"
)
NOVICE_EVIDENCE_RE = re.compile(
    r"新手|小白|第一次|刚入市|刚开户|不懂|求教|请教|请问|老师|大神|"
    r"怎么|如何|为什么|为何|怎么办|能买吗|能不能买|该不该|要不要|"
    r"多少买|怎么看|啥意思|什么意思|解套吗|补仓吗|卖吗"
)
NOVICE_MARKET_CONTEXT_RE = re.compile(
    r"股票|个股|板块|仓位|成本|均线|分红|除权|涨停|跌停|买|卖|补仓|解套"
)
RETAIL_ACTION_RE = re.compile(
    r"买入|卖出|加仓|减仓|补仓|清仓|满仓|半仓|持仓|仓位|成本|做T|"
    r"止损|止盈|抄底|追涨|套牢|解套|看多|看空|突破|支撑|压力|量能|"
    r"均线|主力|庄家|洗盘|出货|筹码|技术面|基本面|龙虎榜|集合竞价|"
    r"涨停|跌停|龙头|资金流|换仓"
)
GRASSROOTS_VOICE_RE = re.compile(
    r"我|咱|兄弟|老师|求问|请教|怎么办|能买吗|能卖吗|该买吗|该卖吗|"
    r"套了|被套|成本|回本|解套|求安慰|梭哈|满仓|加仓|补仓|减仓|"
    r"卖飞|踏空|割肉|拿不住|上车|下车|明天|今天|救命|慌|难受|"
    r"第一次|小白|新手|看不懂|不懂"
)
PROFESSIONAL_STYLE_RE = re.compile(
    r"复盘|投研|研究框架|产业链|估值|基本面|研报|投资策略|策略报告|"
    r"主线梳理|逻辑梳理|不构成投资建议|盘前策略|盘后策略|机构观点|"
    r"核心标的|建议关注|免责声明"
)
PERSONA_EVIDENCE_KEYS = ("baoma", "novice", "retail")
SIGNAL_KEYS = ("optimism", "fomo", "newcomer", "intensity", "attention")
ABSOLUTE_SIGNAL_WEIGHT = {
    "optimism": 0.75,
    "fomo": 0.75,
    "newcomer": 0.70,
    "intensity": 0.75,
    "attention": 0.50,
}


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def round1(value: float) -> float:
    return round(clamp(value), 1)


def safe_int(value: object, default: int = 0) -> int:
    try:
        raw = default if value is None or str(value).strip() == "" else value
        return int(float(str(raw)))
    except (TypeError, ValueError):
        return default


def write_log(message: str) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(CN_TZ).strftime("%Y-%m-%d %H:%M:%S")
    with LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(f"[{stamp}] {message}\n")


def load_json(path: Path, default: object) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def save_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def save_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(value, encoding="utf-8")
    os.replace(temporary, path)


def request_text(
    url: str,
    platform: str,
    attempts: int = 2,
    timeout: int = 12,
) -> str:
    referers = {
        "eastmoney": "https://guba.eastmoney.com/",
        "sina": "https://guba.sina.cn/",
        "ths": "https://t.10jqka.com.cn/",
        "taoguba": "https://www.tgb.cn/quotes/",
    }
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131 Safari/537.36",
        "Referer": referers.get(platform, "https://www.baidu.com/"),
        "Accept": "application/json,text/html;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Accept-Encoding": "identity",
        "Connection": "close",
    }
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            request = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read()
                charset = response.headers.get_content_charset() or "utf-8"
                try:
                    return body.decode(charset)
                except (LookupError, UnicodeDecodeError):
                    return body.decode("utf-8", errors="replace")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(1.2 * (2 ** attempt))
    raise RuntimeError(f"读取失败：{url} ({last_error})")


def request_json(url: str, platform: str, attempts: int = 2, timeout: int = 12) -> Dict[str, object]:
    try:
        payload = json.loads(request_text(url, platform, attempts=attempts, timeout=timeout))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{PLATFORM_NAMES.get(platform, platform)} 返回了非 JSON 内容") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"{PLATFORM_NAMES.get(platform, platform)} 返回结构异常")
    return payload


def clean_text(value: object, max_chars: int = 260) -> str:
    text = html.unescape(str(value or ""))
    text = re.sub(r"<hx_stock[^>]*>\s*stockName:([^,<]+).*?</hx_stock>", r" \1 ", text, flags=re.I | re.S)
    text = re.sub(r"<hx_at[^>]*>.*?</hx_at>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"[\u200b\ufeff]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_chars].rstrip()


def nested_content(value: object) -> str:
    if isinstance(value, dict):
        return clean_text(value.get("content"))
    return clean_text(value)


def normalize_text(text: str) -> str:
    return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]", "", text).lower()


def valid_text(text: str) -> bool:
    compact = normalize_text(text)
    if len(compact) < 4 or compact in LOW_INFORMATION:
        return False
    if any(word in text for word in SPAM_WORDS):
        return False
    marketing_hits = sum(1 for word in MARKETING_WORDS if word in text)
    # 只拦“长文本 + 多个营销线索/明确号召”的组合，不因篇幅删除正常讨论。
    if len(compact) >= 30 and marketing_hits >= 2 and any(word in text for word in MARKETING_CALLS):
        return False
    return True


def grassroots_style_weight(post: Mapping[str, object]) -> float:
    """Estimate ordinary-user expression without guessing anyone's identity or IQ.

    The rule is deliberately symmetric: it changes how much evidence a piece of
    text supplies, never whether bullish or bearish language is preferred.
    Certified/official material is excluded by collectors; this second layer
    reduces the remaining long-form research/self-media style and gives ordinary
    comments, position questions and colloquial first-person language full weight.
    """

    if bool(post.get("certified")):
        return 0.0
    text = str(post.get("title") or "")
    compact_length = max(
        len(normalize_text(text)),
        safe_int(post.get("originalLength")),
    )
    kind = str(post.get("kind") or "post")
    if kind in {"comment", "reply"}:
        weight = 1.0
    elif compact_length <= 240:
        weight = 1.0
    elif compact_length <= 600:
        weight = 0.45
    else:
        weight = 0.10

    stock_tags = max(0, safe_int(post.get("stockTagCount")))
    if stock_tags >= 3:
        weight *= 0.35
    elif stock_tags == 2:
        weight *= 0.70

    professional_hits = len(PROFESSIONAL_STYLE_RE.findall(text))
    if professional_hits:
        weight *= max(0.20, 1.0 - 0.30 * professional_hits)
    if GRASSROOTS_VOICE_RE.search(text) or re.search(r"[?？]", text):
        weight *= 1.15
    return round(max(0.0, min(1.0, weight)), 4)


def parse_time(value: object) -> datetime | None:
    if isinstance(value, (int, float)) or str(value or "").strip().isdigit():
        number = float(value or 0)
        if number > 10_000_000_000:
            number /= 1000.0
        try:
            return datetime.fromtimestamp(number, timezone.utc).astimezone(CN_TZ)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
        return parsed.replace(tzinfo=CN_TZ) if parsed.tzinfo is None else parsed.astimezone(CN_TZ)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=CN_TZ)
        except ValueError:
            continue
    return None


def make_post(
    *,
    post_id: object,
    text: str,
    published: datetime,
    comments: object,
    clicks: object,
    likes: object,
    source_key: str,
    source_code: str,
    source_name: str,
    platform: str,
    author_key: object = "",
    kind: str = "post",
    time_inherited: bool = False,
    parent_post_id: object = "",
) -> Dict[str, object]:
    post = {
        "id": f"{platform}:{post_id}",
        "title": text,
        "published": published,
        "publishTime": published.isoformat(),
        "comments": max(0, safe_int(comments)),
        "clicks": max(0, safe_int(clicks)),
        "likes": max(0, safe_int(likes)),
        "sourceKey": source_key,
        "sourceCode": source_code,
        "source": source_name,
        "platform": platform,
        "platformName": PLATFORM_NAMES[platform],
        "authorKey": str(author_key or ""),
        "kind": kind,
        "timeInherited": bool(time_inherited),
    }
    if parent_post_id:
        post["parentPostId"] = str(parent_post_id)
    return post


def author_daily_admit(
    counts: Dict[Tuple[str, str], int],
    author_key: object,
    published: datetime,
    *,
    limit: int = AUTHOR_DAILY_LIMIT,
) -> bool:
    """Apply the collection cap to an author *and publication date*.

    Applying the cap to a whole page made an active author consume the quota on
    the newest day and silently removed their observations from the preceding
    two scoring days.  The scoring layer already uses the same author/day unit,
    so the collector now preserves that unit as well.
    """

    local = published.replace(tzinfo=CN_TZ) if published.tzinfo is None else published.astimezone(CN_TZ)
    bucket = (str(author_key or "unknown"), local.date().isoformat())
    if counts[bucket] >= max(1, int(limit)):
        return False
    counts[bucket] += 1
    return True


def third_scoring_day(as_of: datetime = NOW) -> date:
    """Oldest A-share session required by the 50/30/20 scoring window."""

    days = scoring_trading_days(as_of)
    return days[-1] if days else scoring_market_day(as_of)


def reached_third_scoring_day(times: Sequence[datetime], as_of: datetime = NOW) -> bool:
    boundary = third_scoring_day(as_of)
    return any(
        (item.replace(tzinfo=CN_TZ) if item.tzinfo is None else item.astimezone(CN_TZ)).date() <= boundary
        for item in times
    )


def page_reaches_third_scoring_day(times: Sequence[datetime], as_of: datetime = NOW) -> bool:
    """Use the chronological tail so stale pinned posts cannot stop pagination."""

    if not times:
        return False
    boundary = third_scoring_day(as_of)
    tail = list(times[-min(10, len(times)):])
    reached = sum(
        1
        for item in tail
        if (item.replace(tzinfo=CN_TZ) if item.tzinfo is None else item.astimezone(CN_TZ)).date()
        <= boundary
    )
    return reached >= min(3, len(tail))


def collection_metadata(
    *,
    raw_times: Sequence[datetime],
    posts: Sequence[Mapping[str, object]],
    raw_rows: int,
    page_count: int,
    cap_hit: bool | None,
    chronological: bool,
    fresh_fetched_now: bool,
    fetched_at: object = None,
    third_day_reached: bool | None = None,
) -> Dict[str, object]:
    """Build explicit, cache-safe evidence about source coverage.

    ``rawRows`` describes server rows before filtering. ``coverageDays`` and
    the time bounds use raw row timestamps when available, so an excluded
    verified/spam post still proves that the pagination cursor reached a day.
    ``cacheAge`` is seconds as of this process' ``NOW`` and is never presented
    as source completeness.
    """

    observed: List[datetime] = []
    for value in raw_times:
        if isinstance(value, datetime):
            observed.append(value.replace(tzinfo=CN_TZ) if value.tzinfo is None else value.astimezone(CN_TZ))
    if not observed:
        for post in posts:
            value = post.get("published")
            if not isinstance(value, datetime):
                value = parse_time(post.get("publishTime"))
            if isinstance(value, datetime):
                observed.append(value.replace(tzinfo=CN_TZ) if value.tzinfo is None else value.astimezone(CN_TZ))

    fetched = parse_time(fetched_at) or NOW
    cache_age = max(0, int((NOW - fetched).total_seconds()))
    coverage_days = sorted({item.date().isoformat() for item in observed}, reverse=True)
    return {
        "rawRows": max(0, int(raw_rows)),
        "pageCount": max(0, int(page_count)),
        "oldestSeen": min(observed).isoformat() if observed else None,
        "newestSeen": max(observed).isoformat() if observed else None,
        "capHit": cap_hit,
        "chronological": bool(chronological),
        "coverageDays": coverage_days,
        "thirdTradingDayReached": (
            bool(third_day_reached)
            if third_day_reached is not None
            else reached_third_scoring_day(observed) if observed and chronological else False
        ),
        "freshFetchedNow": bool(fresh_fetched_now),
        "cacheAge": cache_age,
        "cacheAgeUnit": "seconds",
    }


def market_collection_policy(spec: Mapping[str, object]) -> Dict[str, object]:
    is_market = bool(spec.get("marketWindow"))
    return {
        "marketWindow": is_market,
        "pageLimit": MARKET_PAGE_LIMIT if is_market else COLLECTOR_PAGE_LIMIT,
        "deadline": time.monotonic() + MARKET_TIME_BUDGET_SECONDS if is_market else None,
    }


def collection_timeout(policy: Mapping[str, object], maximum: float = 10.0) -> float:
    deadline = policy.get("deadline")
    if deadline is None:
        return maximum
    remaining = float(deadline) - time.monotonic()
    if remaining <= 0:
        raise RuntimeError("market_time_budget_exhausted")
    return min(maximum, remaining)


def page_boundary(times: Sequence[datetime], row_count: int) -> datetime | None:
    # Every row must have a timestamp; max(tail) prevents an old pinned row
    # from falsely proving that the intervening stream has been traversed.
    if not times or len(times) != row_count:
        return None
    return max(times[-min(10, len(times)):])


def fetch_eastmoney(spec: Mapping[str, object]) -> Dict[str, object]:
    code = str(spec["code"])
    label = str(spec["name"])
    source_key = str(spec["key"])
    page_url = f"https://guba.eastmoney.com/list,{code}.html"
    all_rows: List[Dict[str, object]] = []
    raw_times: List[datetime] = []
    raw_rows = 0
    page_count = 0
    cap_hit: bool | None = False
    coverage_status = "unknown"
    pagination_mode = "page"
    target_reached = False
    policy = market_collection_policy(spec)
    window_start_covered = False
    chronological_boundary: datetime | None = None
    seen_pages: set[Tuple[str, ...]] = set()

    for page_number in range(1, int(policy["pageLimit"]) + 1):
        if policy["marketWindow"]:
            if page_number > 1:
                time.sleep(min(MARKET_PAGE_INTERVAL_SECONDS, max(0.0, float(policy["deadline"]) - time.monotonic())))
            if time.monotonic() >= float(policy["deadline"]):
                coverage_status = "time_budget_exhausted"
                cap_hit = True
                break
        payload: Dict[str, object] | None = None
        last_error: Exception | None = None
        query = urllib.parse.urlencode({
            "code": code,
            "sorttype": 1,
            "ps": EASTMONEY_PAGE_SIZE,
            "p": page_number,
            "from": "CommonBaPost",
            "deviceid": "2f7f40de-2fb0-4d84-8a31-111111111111",
            "version": 200,
            "product": "Guba",
            "plat": "Web",
        })
        api_url = "https://gbapi.eastmoney.com/webarticlelist/api/Article/Articlelist?" + query
        for attempt in range(3):
            try:
                candidate = request_json(api_url, "eastmoney", attempts=1, timeout=collection_timeout(policy))
                message = str(candidate.get("me") or "").strip()
                rows_value = candidate.get("re")
                # Quiet boards use this terminal marker even on page one.
                if message == "到底啦":
                    payload = {**candidate, "re": rows_value if isinstance(rows_value, list) else []}
                    break
                if not isinstance(rows_value, list):
                    raise RuntimeError(str(candidate.get("me") or "帖子列表为空"))
                if message not in ("", "操作成功"):
                    raise RuntimeError(message)
                payload = candidate
                break
            except RuntimeError as exc:
                last_error = exc
                if policy["marketWindow"] and time.monotonic() >= float(policy["deadline"]):
                    break
                if attempt < 2:
                    delay = 1.0 + attempt
                    if policy["marketWindow"]:
                        delay = min(delay, max(0.0, float(policy["deadline"]) - time.monotonic()))
                    time.sleep(delay)

        if payload is None:
            # The public HTML fallback only exposes one embedded page.  It is
            # useful as a last resort, but is explicitly marked as completeness
            # unknown instead of pretending the three-day window was traversed.
            if page_number > 1:
                if policy["marketWindow"]:
                    coverage_status = "partial_fetch_failure"
                    cap_hit = None
                    break
                raise RuntimeError(str(last_error or f"{label} 第 {page_number} 页读取失败"))
            try:
                page = request_text(page_url, "eastmoney", attempts=1, timeout=collection_timeout(policy))
                if "身份核实" in page:
                    raise RuntimeError(f"{label} 触发站点临时身份核实")
                matched = re.search(r"var\s+article_list\s*=\s*(\{.*?\});", page, re.DOTALL)
                if not matched:
                    raise RuntimeError(f"{label} 页面没有公开帖子列表")
                payload = json.loads(matched.group(1))
                pagination_mode = "html_single_page_fallback"
                cap_hit = None
            except (RuntimeError, json.JSONDecodeError) as exc:
                raise RuntimeError(str(last_error or exc)) from exc

        rows = payload.get("re") if isinstance(payload.get("re"), list) else []
        signature = tuple(str(row.get("post_id")) for row in rows if isinstance(row, Mapping))
        if policy["marketWindow"] and signature and signature in seen_pages:
            coverage_status = "repeated_page"
            cap_hit = None
            break
        seen_pages.add(signature)
        page_count += 1
        raw_rows += len(rows)
        page_times: List[datetime] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            all_rows.append(row)
            observed = parse_time(row.get("post_publish_time"))
            if observed is not None:
                raw_times.append(observed)
                page_times.append(observed)

        reached_target = page_reaches_third_scoring_day(page_times)
        total_count = safe_int(payload.get("count"), 0)
        source_exhausted = (
            not rows
            or (not policy["marketWindow"] and len(rows) < EASTMONEY_PAGE_SIZE)
            or str(payload.get("me") or "") == "到底啦"
            or (total_count > 0 and total_count <= page_number * EASTMONEY_PAGE_SIZE)
        )
        target_reached = target_reached or reached_target
        boundary = page_boundary(page_times, len(rows))
        if pagination_mode == "page" and boundary is not None:
            chronological_boundary = boundary
        window_start_covered = (
            chronological_boundary is not None
            and chronological_boundary.date() < third_scoring_day()
        )
        if (window_start_covered if policy["marketWindow"] else reached_target):
            coverage_status = "window_start_covered" if policy["marketWindow"] else "third_trading_day_reached"
            break
        if pagination_mode != "page":
            coverage_status = "single_page_fallback_unknown"
            break
        if source_exhausted:
            window_start_covered = bool(policy["marketWindow"])
            coverage_status = "source_exhausted_before_third_trading_day"
            break
        if page_number == int(policy["pageLimit"]):
            cap_hit = True
            coverage_status = "page_cap_reached_before_third_trading_day"

    posts: List[Dict[str, object]] = []
    author_day_counts: Dict[Tuple[str, str], int] = defaultdict(int)
    seen_ids: set[str] = set()
    for row in all_rows:
        if safe_int(row.get("post_type"), -1) != 0:
            continue
        nickname = str(row.get("user_nickname") or "").strip()
        if not nickname or str(row.get("post_source_id") or "").strip():
            continue
        if any(word in nickname for word in AUTHOR_BLOCK_WORDS):
            continue
        user_key = str(row.get("user_id") or nickname)
        text = clean_text(row.get("post_title"))
        published = parse_time(row.get("post_publish_time"))
        if not valid_text(text) or published is None:
            continue
        post_id = str(row.get("post_id") or normalize_text(text))
        if post_id in seen_ids:
            continue
        seen_ids.add(post_id)
        if not author_daily_admit(author_day_counts, user_key, published):
            continue
        posts.append(make_post(
            post_id=post_id,
            text=text,
            published=published,
            comments=row.get("post_comment_count"),
            clicks=row.get("post_click_count"),
            likes=0,
            source_key=source_key,
            source_code=code,
            source_name=label,
            platform="eastmoney",
            author_key=user_key,
        ))
    return {
        "key": source_key,
        "code": code,
        "name": label,
        "platform": "eastmoney",
        "url": page_url,
        "posts": posts,
        "fetchedAt": NOW.isoformat(),
        "fresh": True,
        **collection_metadata(
            raw_times=raw_times,
            posts=posts,
            raw_rows=raw_rows,
            page_count=page_count,
            cap_hit=cap_hit,
            chronological=True,
            fresh_fetched_now=True,
            fetched_at=NOW,
            third_day_reached=target_reached,
        ),
        "coverageStatus": coverage_status,
        "paginationMode": pagination_mode,
        "rawRowsEstimated": False,
        "marketWindowPolicyVersion": MARKET_WINDOW_POLICY_VERSION if policy["marketWindow"] else None,
        "windowStartCovered": window_start_covered,
        "chronologicalBoundary": chronological_boundary.isoformat() if chronological_boundary else None,
        "pageLimit": policy["pageLimit"],
        "timeBudgetSeconds": MARKET_TIME_BUDGET_SECONDS if policy["marketWindow"] else None,
    }


SINA_BIDS: Dict[str, object] = dict(load_json(SINA_BID_CACHE_PATH, {}) or {})


def sina_symbol(code: str) -> str:
    if re.fullmatch(r"(?:sh|sz|bj)\d{6}", code.lower()):
        return code.lower()
    if code.startswith(("5", "6", "9")):
        return "sh" + code
    if code.startswith(("4", "8")):
        return "bj" + code
    return "sz" + code


def discover_sina_bid(code: str) -> str:
    cached = SINA_BIDS.get(code)
    if cached:
        return str(cached)
    symbol = sina_symbol(code)
    page_url = f"https://guba.sina.cn/?s=bar&name={symbol}&from=redirect"
    page = request_text(page_url, "sina", attempts=1, timeout=12)
    patterns = (
        r'["\']bid["\']\s*:\s*["\']?(\d+)',
        r"\bbid\s*[:=]\s*['\"]?(\d+)",
        r"data-bid=['\"](\d+)",
    )
    for pattern in patterns:
        matched = re.search(pattern, page, re.I)
        if matched:
            SINA_BIDS[code] = matched.group(1)
            return matched.group(1)
    raise RuntimeError(f"新浪股吧未找到 {code} 的公开论坛编号")


def truthy(value: object) -> bool:
    return value is True or str(value or "").strip().lower() in {"1", "true", "yes"}


def fetch_sina(spec: Mapping[str, object]) -> Dict[str, object]:
    code = str(spec["code"])
    stock_name = str(spec["name"])
    label = f"{stock_name}新浪股吧"
    source_key = str(spec["key"])
    symbol = sina_symbol(code)
    page_url = f"https://guba.sina.cn/?s=bar&name={symbol}&from=redirect"
    bid = discover_sina_bid(code)
    all_rows: List[Dict[str, object]] = []
    raw_times: List[datetime] = []
    raw_rows = 0
    page_count = 0
    cap_hit: bool | None = False
    coverage_status = "unknown"
    target_reached = False
    policy = market_collection_policy(spec)
    window_start_covered = False
    chronological_boundary: datetime | None = None
    seen_pages: set[Tuple[str, ...]] = set()
    cursor_value = ""
    cursor_tid = ""
    seen_cursors: set[Tuple[str, str]] = set()
    api_url = ""

    for page_number in range(1, int(policy["pageLimit"]) + 1):
        if policy["marketWindow"]:
            if page_number > 1:
                time.sleep(min(MARKET_PAGE_INTERVAL_SECONDS, max(0.0, float(policy["deadline"]) - time.monotonic())))
            if time.monotonic() >= float(policy["deadline"]):
                coverage_status = "time_budget_exhausted"
                cap_hit = True
                break
        query: Dict[str, object] = {"s": "h5bar", "bid": bid, "num": SINA_PAGE_SIZE}
        if cursor_value and cursor_tid:
            query["relate_value"] = cursor_value
            query["relate_tid"] = cursor_tid
        api_url = "https://guba.sina.cn/api/?" + urllib.parse.urlencode(query)
        try:
            payload = request_json(api_url, "sina", attempts=1 if policy["marketWindow"] else 2, timeout=collection_timeout(policy, 12.0))
        except RuntimeError:
            if policy["marketWindow"] and all_rows:
                coverage_status = "partial_fetch_failure"
                cap_hit = None
                break
            raise
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        rows = data.get("threads") if isinstance(data, dict) else []
        if not isinstance(rows, list):
            raise RuntimeError(f"{label} 返回结构异常")

        signature = tuple(str(row.get("tid")) for row in rows if isinstance(row, Mapping))
        if policy["marketWindow"] and signature and signature in seen_pages:
            coverage_status = "repeated_page"
            cap_hit = None
            break
        seen_pages.add(signature)
        page_count += 1
        raw_rows += len(rows)
        page_times: List[datetime] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            all_rows.append(row)
            observed = parse_time(row.get("timestamp") or row.get("lastctime") or row.get("ctime"))
            if observed is not None:
                raw_times.append(observed)
                page_times.append(observed)

        reached_target = page_reaches_third_scoring_day(page_times)
        target_reached = target_reached or reached_target
        boundary = page_boundary(page_times, len(rows))
        if boundary is not None:
            chronological_boundary = boundary
        window_start_covered = (
            chronological_boundary is not None
            and chronological_boundary.date() < third_scoring_day()
        )
        if (window_start_covered if policy["marketWindow"] else reached_target):
            coverage_status = "window_start_covered" if policy["marketWindow"] else "third_trading_day_reached"
            break
        # A short page may still have a valid continuation cursor (e.g. 49/50).
        # Only an empty page proves exhaustion; stalled cursors remain unknown.
        if not rows:
            window_start_covered = bool(policy["marketWindow"])
            coverage_status = "source_exhausted_before_third_trading_day"
            break

        last_row = rows[-1] if isinstance(rows[-1], dict) else {}
        next_value = str(last_row.get("timestamp") or "")
        next_tid = str(last_row.get("tid") or "")
        next_cursor = (next_value, next_tid)
        if not all(next_cursor) or next_cursor in seen_cursors or next_cursor == (cursor_value, cursor_tid):
            cap_hit = None
            coverage_status = "cursor_stalled_before_third_trading_day"
            break
        seen_cursors.add(next_cursor)
        cursor_value, cursor_tid = next_cursor
        if page_number == int(policy["pageLimit"]):
            cap_hit = True
            coverage_status = "page_cap_reached_before_third_trading_day"

    posts: List[Dict[str, object]] = []
    author_day_counts: Dict[Tuple[str, str], int] = defaultdict(int)
    seen_ids: set[str] = set()
    for row in all_rows:
        user = row.get("user") if isinstance(row.get("user"), dict) else {}
        if truthy(user.get("verified")):
            continue
        nickname = str(user.get("nick") or user.get("name") or "")
        if any(word in nickname for word in AUTHOR_BLOCK_WORDS):
            continue
        user_key = str(user.get("uid") or nickname or row.get("tid") or "unknown")
        text = clean_text(row.get("title") or row.get("content") or row.get("long_content"))
        published = parse_time(row.get("timestamp") or row.get("lastctime") or row.get("ctime"))
        if not valid_text(text) or published is None:
            continue
        post_id = str(row.get("tid") or normalize_text(text))
        if post_id in seen_ids:
            continue
        seen_ids.add(post_id)
        if not author_daily_admit(author_day_counts, user_key, published):
            continue
        posts.append(make_post(
            post_id=post_id,
            text=text,
            published=published,
            comments=row.get("reply"),
            clicks=row.get("views"),
            likes=row.get("like"),
            source_key=source_key,
            source_code=code,
            source_name=label,
            platform="sina",
            author_key=user_key,
        ))
    return {
        "key": source_key,
        "code": code,
        "name": label,
        "platform": "sina",
        "url": page_url,
        "posts": posts,
        "fetchedAt": NOW.isoformat(),
        "fresh": True,
        **collection_metadata(
            raw_times=raw_times,
            posts=posts,
            raw_rows=raw_rows,
            page_count=page_count,
            cap_hit=cap_hit,
            chronological=True,
            fresh_fetched_now=True,
            fetched_at=NOW,
            third_day_reached=target_reached,
        ),
        "coverageStatus": coverage_status,
        "paginationMode": "cursor",
        "rawRowsEstimated": False,
        "marketWindowPolicyVersion": MARKET_WINDOW_POLICY_VERSION if policy["marketWindow"] else None,
        "windowStartCovered": window_start_covered,
        "chronologicalBoundary": chronological_boundary.isoformat() if chronological_boundary else None,
        "pageLimit": policy["pageLimit"],
        "timeBudgetSeconds": MARKET_TIME_BUDGET_SECONDS if policy["marketWindow"] else None,
    }


THS_CONFIG: Dict[str, object] = dict(load_json(THS_CONFIG_CACHE_PATH, {}) or {})


def ths_market_fallback(code: str) -> int:
    if code == "1A0001":
        return 16
    return 17 if code.startswith(("5", "6", "9")) else 33


def ths_get_json(path_and_query: str) -> Dict[str, object]:
    # 只读取同花顺公开社区端点，不传账号、Cookie 或个人参数。
    # 该站在 Windows 无人值守环境中的 HTTPS 经常先挂起再断开，而同一
    # 公开 JSON 的 HTTP 入口实测稳定；优先稳定入口可把整轮35个板块源
    # 从几十分钟降到数分钟。失败时仍回退 HTTPS。
    global THS_API_BASE
    if THS_API_BASE:
        try:
            return request_json(THS_API_BASE + path_and_query, "ths", attempts=1, timeout=8)
        except RuntimeError:
            THS_API_BASE = None
    last_error: RuntimeError | None = None
    for base in ("http://c.10jqka.com.cn", "https://c.10jqka.com.cn"):
        try:
            payload = request_json(base + path_and_query, "ths", attempts=1, timeout=8)
            THS_API_BASE = base
            return payload
        except RuntimeError as exc:
            last_error = exc
    raise last_error or RuntimeError("同花顺公开社区端点不可用")


def ths_cached_config(cache_key: str) -> Dict[str, object] | None:
    value = THS_CONFIG.get(cache_key)
    if not isinstance(value, dict):
        return None
    discovered = parse_time(value.get("discoveredAt"))
    if discovered is None or NOW - discovered > timedelta(days=7):
        return None
    return dict(value)


def discover_ths_forum(spec: Mapping[str, object]) -> Dict[str, object]:
    cache_key = str(spec["key"])
    cached = ths_cached_config(cache_key)
    if cached:
        return cached
    forum_code = str(spec["code"])
    market_id = safe_int(spec.get("marketId"), ths_market_fallback(forum_code))
    forum_name = str(spec.get("name") or forum_code)
    related_scenes: List[Dict[str, object]] = []
    discovery_status = "direct_only"
    if bool(spec.get("preferScene", True)):
        query = urllib.parse.urlencode({
            "code": forum_code,
            "marketId": market_id,
            "source": "stock",
        })
        try:
            payload = ths_get_json("/lgt/cache/open/api/forum/v2/index?" + query)
            data = payload.get("data") if isinstance(payload.get("data"), Mapping) else {}
            configs = (
                data.get("function_configs")
                if isinstance(data.get("function_configs"), Mapping) else {}
            )
            raw_scenes = configs.get("related_scenes") if isinstance(configs, Mapping) else []
            for scene in raw_scenes if isinstance(raw_scenes, list) else []:
                if not isinstance(scene, Mapping):
                    continue
                scene_code = str(scene.get("scene_code") or "").strip()
                scene_market = safe_int(scene.get("scene_market"), 0)
                if not scene_code or not scene_market:
                    continue
                related_scenes.append({
                    "forumCode": scene_code,
                    "marketId": scene_market,
                    "forumName": str(scene.get("scene_name") or scene_code),
                    "talkCount": max(0, safe_int(scene.get("talk_num"))),
                })
            discovery_status = "metadata_with_related_scenes"
        except RuntimeError:
            # 关联社区是增强项；概念社区本身仍可独立读取。
            discovery_status = "metadata_unavailable_direct_fallback"
    result = {
        "forumCode": forum_code,
        "marketId": market_id,
        "forumName": forum_name,
        "relatedScenes": related_scenes,
        "discoveryStatus": discovery_status,
        "discoveredAt": NOW.isoformat(),
        "directFallback": not bool(related_scenes),
    }
    THS_CONFIG[cache_key] = result
    return result


def fetch_ths_hot_pages(
    forum_code: str,
    market_id: int,
) -> Tuple[List[Dict[str, object]], int, bool, str]:
    """Read a bounded ranked feed using THS' public last_score cursor."""

    collected: List[Dict[str, object]] = []
    seen: set[str] = set()
    last_score: object = None
    page_count = 0
    has_more = False
    first_path = ""
    for _ in range(THS_CURSOR_PAGE_LIMIT):
        params: Dict[str, object] = {
            "code": forum_code,
            "market_id": market_id,
            "page": 1,
            "page_size": 15,
        }
        if last_score not in (None, ""):
            params["last_score"] = last_score
        path = "/lgt/post/open/api/forum/content/v1/hot_feed?" + urllib.parse.urlencode(params)
        if not first_path:
            first_path = path
        payload = ths_get_json(path)
        if safe_int(payload.get("status_code"), -1) != 0:
            raise RuntimeError(str(payload.get("status_msg") or "同花顺社区读取失败"))
        data = payload.get("data") if isinstance(payload.get("data"), Mapping) else {}
        rows = data.get("feed") if isinstance(data.get("feed"), list) else []
        page_count += 1
        added = 0
        for row in rows:
            if not isinstance(row, dict):
                continue
            info = row.get("info") if isinstance(row.get("info"), Mapping) else {}
            identity = str(info.get("id") or hashlib.sha256(
                json.dumps(row, ensure_ascii=False, sort_keys=True).encode("utf-8")
            ).hexdigest())
            if identity in seen:
                continue
            seen.add(identity)
            collected.append(row)
            added += 1
        has_more = bool(data.get("has_more"))
        next_score = data.get("last_score")
        if not rows or not has_more or not added or next_score in (None, "", last_score):
            break
        last_score = next_score
        time.sleep(0.8)
    return collected, page_count, has_more, first_path


def fetch_ths(spec: Mapping[str, object]) -> Dict[str, object]:
    # 同花顺公开端点低频读取；概念与其关联场景最终仍只形成一个来源单位。
    time.sleep(0.5)
    config = discover_ths_forum(spec)
    forum_code = str(config["forumCode"])
    forum_name = str(config.get("forumName") or spec.get("name") or forum_code)
    label = f"同花顺{forum_name}"
    source_key = str(spec["key"])
    entries = [{
        "forumCode": forum_code,
        "marketId": safe_int(config["marketId"]),
        "forumName": forum_name,
        "layer": "concept",
    }]
    for scene in config.get("relatedScenes", []):
        if isinstance(scene, Mapping):
            entries.append({**dict(scene), "layer": "related_scene"})

    feed_rows: List[Tuple[Dict[str, object], Mapping[str, object]]] = []
    page_count = 0
    cursor_has_more = False
    first_path = ""
    for entry in entries:
        rows, pages, has_more, path = fetch_ths_hot_pages(
            str(entry["forumCode"]), safe_int(entry["marketId"])
        )
        page_count += pages
        cursor_has_more = cursor_has_more or has_more
        if not first_path:
            first_path = path
        feed_rows.extend((row, entry) for row in rows)
        if entry is not entries[-1]:
            time.sleep(0.8)
    api_url = "https://c.10jqka.com.cn" + first_path

    posts: List[Dict[str, object]] = []
    author_day_counts: Dict[Tuple[str, str], int] = defaultdict(int)
    seen_ids: set[str] = set()
    raw_times: List[datetime] = []
    ordinary_post_count = 0
    ordinary_comment_count = 0
    related_scene_post_count = 0
    for row, entry in feed_rows:
        if not isinstance(row, dict):
            continue
        info = row.get("info") if isinstance(row.get("info"), dict) else {}
        author = row.get("author") if isinstance(row.get("author"), dict) else {}
        stat = row.get("stat") if isinstance(row.get("stat"), dict) else {}
        published = parse_time(info.get("ctime"))
        if published is None:
            continue
        raw_times.append(published)
        post_id = str(info.get("id") or "")
        author_name = str(author.get("name") or "")
        author_key = str(author.get("id") or author_name or post_id)
        is_ordinary_post = (
            str(info.get("state") or "") == "passAudit"
            and str(info.get("type") or "") == "post"
            and safe_int(info.get("community_biz_type"), -1) == 1
            and not author.get("identity_tag")
            and not author.get("medals")
            and not author.get("medal")
            and not any(word in author_name for word in AUTHOR_BLOCK_WORDS)
            and not row.get("forward")
        )
        if is_ordinary_post and post_id not in seen_ids:
            def full_nested(value: object) -> str:
                return clean_text(value.get("content"), max_chars=2000) if isinstance(value, Mapping) else clean_text(value, max_chars=2000)
            full_text = " ".join(filter(None, (full_nested(row.get("title")), full_nested(row.get("abstract")))))
            text = clean_text(full_text)
            if valid_text(text) and author_daily_admit(author_day_counts, author_key, published, limit=1):
                seen_ids.add(post_id)
                post = make_post(
                    post_id=post_id or normalize_text(text),
                    text=text,
                    published=published,
                    comments=stat.get("comment_num"),
                    clicks=stat.get("forward_num"),
                    likes=stat.get("like_num"),
                    source_key=source_key,
                    source_code=forum_code,
                    source_name=label,
                    platform="ths",
                    author_key=author_key,
                )
                post.update({
                    "originalLength": len(normalize_text(full_text)),
                    "stockTagCount": sum(
                        1 for tag in row.get("tags", [])
                        if isinstance(tag, Mapping) and str(tag.get("type") or "").lower() == "stock"
                    ) if isinstance(row.get("tags"), list) else 0,
                    "communityLayer": entry.get("layer"),
                    "communityName": entry.get("forumName"),
                    "certified": False,
                })
                posts.append(post)
                ordinary_post_count += 1
                if entry.get("layer") == "related_scene":
                    related_scene_post_count += 1

        comment_block = row.get("comment") if isinstance(row.get("comment"), dict) else {}
        comments = comment_block.get("comments") if isinstance(comment_block.get("comments"), list) else []
        for index, comment in enumerate(comments):
            if not isinstance(comment, dict) or str(comment.get("tag") or "") != "ordinary":
                continue
            nickname = str(comment.get("nickname") or "")
            if any(word in nickname for word in AUTHOR_BLOCK_WORDS):
                continue
            user_key = str(comment.get("uid") or nickname or f"{post_id}:{index}")
            text = clean_text(comment.get("content"), max_chars=180)
            if not valid_text(text):
                continue
            comment_published = parse_time(
                comment.get("ctime")
                or comment.get("create_time")
                or comment.get("timestamp")
                or comment.get("publish_time")
            )
            time_inherited = comment_published is None
            if comment_published is None:
                comment_published = published
            else:
                raw_times.append(comment_published)
            if not author_daily_admit(author_day_counts, user_key, comment_published, limit=1):
                continue
            comment_id = str(comment.get("id") or f"{post_id}:comment:{index}")
            if comment_id in seen_ids:
                continue
            seen_ids.add(comment_id)
            post = make_post(
                post_id=comment_id,
                text=text,
                published=comment_published,
                comments=0,
                clicks=0,
                likes=0,
                source_key=source_key,
                source_code=forum_code,
                source_name=label,
                platform="ths",
                author_key=user_key,
                kind="comment",
                time_inherited=time_inherited,
                parent_post_id=post_id,
            )
            post.update({
                "originalLength": len(normalize_text(text)),
                "stockTagCount": 0,
                "communityLayer": entry.get("layer"),
                "communityName": entry.get("forumName"),
                "certified": False,
            })
            posts.append(post)
            ordinary_comment_count += 1
    if not feed_rows:
        raise RuntimeError(f"{label} 公开热门流为空")
    return {
        "key": source_key,
        "code": forum_code,
        "name": label,
        "platform": "ths",
        "url": api_url,
        "posts": posts,
        "fetchedAt": NOW.isoformat(),
        "fresh": True,
        **collection_metadata(
            raw_times=raw_times,
            posts=posts,
            raw_rows=len(feed_rows),
            page_count=page_count,
            cap_hit=cursor_has_more,
            chronological=False,
            fresh_fetched_now=True,
            fetched_at=NOW,
        ),
        "coverageStatus": "non_chronological_hot_feed",
        "paginationMode": "last_score_cursor_bounded",
        "feedType": "concept_plus_related_scene_hot_feed",
        "rawRowsEstimated": False,
        "hotFeedCensored": True,
        "ordinaryPostCount": ordinary_post_count,
        "ordinaryCommentCount": ordinary_comment_count,
        "relatedScenePostCount": related_scene_post_count,
        "relatedScenes": [
            {
                "code": entry.get("forumCode"),
                "marketId": entry.get("marketId"),
                "name": entry.get("forumName"),
                "layer": entry.get("layer"),
            }
            for entry in entries
        ],
        "attentionEligible": False,
        "discoveryStatus": config.get("discoveryStatus"),
    }


def fetch_taoguba(spec: Mapping[str, object]) -> Dict[str, object]:
    """Read the anonymous latest-discussion block embedded in a Taoguba quote page.

    The public page exposes 20 newest rows.  Its genuine continuation endpoint
    requires login, so the collector explicitly marks the result as censored
    instead of claiming complete three-day volume.  Daily cache accumulation
    extends the rolling evidence without bypassing that access boundary.
    """

    code = str(spec["code"])
    prefix = "sh" if code.startswith(("5", "6", "9")) else "sz"
    full_code = prefix + code
    source_key = str(spec["key"])
    source_name = f"淘股吧{spec.get('name') or code}讨论"
    url = f"https://www.tgb.cn/quotes/{full_code}"
    page = request_text(url, "taoguba", attempts=2, timeout=12)
    matched = re.search(
        r"var\s+coolAttr\s*=\s*(\[.*?\]);\s*var\s+tempFeedID",
        page,
        re.DOTALL,
    )
    if not matched:
        # Retain a slightly looser fallback for harmless front-end whitespace/
        # variable-order changes, but never scrape rendered prose heuristically.
        matched = re.search(r"var\s+coolAttr\s*=\s*(\[.*?\])\s*;", page, re.DOTALL)
    if not matched:
        raise RuntimeError(f"{source_name} 页面没有公开最新讨论数据")
    try:
        raw_value = json.loads(matched.group(1))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{source_name} 最新讨论结构异常") from exc
    rows = raw_value if isinstance(raw_value, list) else []
    if not rows:
        raise RuntimeError(f"{source_name} 公开最新讨论为空")

    posts: List[Dict[str, object]] = []
    raw_times: List[datetime] = []
    seen_ids: set[str] = set()
    author_day_counts: Dict[Tuple[str, str], int] = defaultdict(int)
    excluded_certified = 0
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        published = parse_time(row.get("actionDate"))
        if published is None:
            continue
        raw_times.append(published)
        user_title = row.get("userTitle") if isinstance(row.get("userTitle"), Mapping) else {}
        if safe_int(row.get("auth"), 0) > 0 or str(user_title.get("title") or "").strip():
            excluded_certified += 1
            continue
        if str(row.get("deleteFlag") or "N").upper() not in {"", "N", "0", "FALSE"}:
            continue
        author_name = clean_text(row.get("userName"), max_chars=60)
        if any(word in author_name for word in AUTHOR_BLOCK_WORDS):
            excluded_certified += 1
            continue
        post_id = str(row.get("rID") or row.get("riD") or row.get("newTopicID") or "")
        if not post_id or post_id in seen_ids:
            continue
        raw_subject = str(row.get("subject") or "")
        raw_body = str(row.get("body") or "")
        raw_html = " ".join(filter(None, (raw_subject, raw_body)))
        kind = "reply" if str(row.get("rType") or row.get("rtype") or "").upper() == "R" else "post"
        text = clean_text(raw_body if kind == "reply" else raw_html)
        if not valid_text(text):
            continue
        author_key = str(row.get("userID") or author_name or post_id)
        if not author_daily_admit(author_day_counts, author_key, published, limit=2):
            continue
        seen_ids.add(post_id)
        post = make_post(
            post_id=post_id,
            text=text,
            published=published,
            comments=row.get("replyNum"),
            clicks=row.get("viewNum"),
            likes=row.get("usefulNum"),
            source_key=source_key,
            source_code=code,
            source_name=source_name,
            platform="taoguba",
            author_key=author_key,
            kind=kind,
        )
        post.update({
            "originalLength": len(normalize_text(clean_text(raw_html, max_chars=4000))),
            "stockTagCount": len(re.findall(
                r"<font[^>]*color\s*=\s*['\"]?#?4793F1", raw_html, re.I
            )),
            "certified": False,
            "anonymousPublicInitialPage": True,
        })
        posts.append(post)

    third_reached = reached_third_scoring_day(raw_times)
    return {
        "key": source_key,
        "code": code,
        "name": source_name,
        "platform": "taoguba",
        "url": url,
        "posts": posts,
        "fetchedAt": NOW.isoformat(),
        "fresh": True,
        **collection_metadata(
            raw_times=raw_times,
            posts=posts,
            raw_rows=len(rows),
            page_count=1,
            cap_hit=len(rows) >= TAOGUBA_PAGE_SIZE,
            chronological=True,
            fresh_fetched_now=True,
            fetched_at=NOW,
            third_day_reached=third_reached,
        ),
        "coverageStatus": "third_day_reached" if third_reached else "anonymous_page_cap_lower_bound",
        "paginationMode": "anonymous_initial_page_only",
        "feedType": "latest_stock_discussion",
        "rawRowsEstimated": False,
        "volumeIsLowerBound": True,
        "excludedCertifiedCount": excluded_certified,
        "attentionEligible": True,
    }


def build_source_specs() -> Tuple[Dict[str, Dict[str, object]], List[str], Dict[str, List[str]]]:
    """生成每日采集计划。

    东方财富、新浪、淘股吧分别读取三家代表公司的个股社区；同花顺读取
    明确配置的板块社区及其公开关联场景。入口数只增加证据覆盖，正式聚合
    仍由四个固定25%渠道席位控制。
    """

    def stock_pairs(value: object) -> List[Tuple[str, str]]:
        rows: List[Tuple[str, str]] = []
        if isinstance(value, Mapping):
            value = [value]
        elif (
            isinstance(value, (list, tuple))
            and len(value) >= 2
            and not isinstance(value[0], (list, tuple, Mapping))
        ):
            value = [value]
        if not isinstance(value, (list, tuple)):
            return rows
        for item in value:
            if isinstance(item, Mapping):
                code = str(item.get("code") or "").strip()
                name = str(item.get("name") or code).strip()
            elif isinstance(item, (list, tuple)) and len(item) >= 2:
                code = str(item[0]).strip()
                name = str(item[1]).strip()
            else:
                continue
            if code:
                rows.append((code, name or code))
        return rows

    specs: Dict[str, Dict[str, object]] = {}
    market_keys: List[str] = []
    sector_keys: Dict[str, List[str]] = {}

    for code, name in MARKET_EASTMONEY_SOURCES:
        key = f"eastmoney:{code}"
        specs[key] = {"key": key, "platform": "eastmoney", "code": code, "name": name, "marketWindow": True}
        market_keys.append(key)
    ths_market_key = "ths:market:1A0001"
    specs[ths_market_key] = {
        "key": ths_market_key,
        "platform": "ths",
        "code": "1A0001",
        "name": "上证指数讨论",
        "marketId": 16,
        "preferScene": False,
    }
    market_keys.append(ths_market_key)

    for symbol, name in MARKET_SINA_SOURCES:
        key = f"sina:{symbol}"
        specs[key] = {"key": key, "platform": "sina", "code": symbol, "name": name, "marketWindow": True}
        market_keys.append(key)

    for sector_key, definition in SECTORS.items():
        keys: List[str] = []
        source_stocks = stock_pairs(definition.get("sourceStocks") or definition.get("stocks"))
        if not source_stocks:
            source_stocks = stock_pairs(definition.get("representativeCompanies"))[:2]
        if not source_stocks:
            raise ValueError(f"板块 {sector_key} 没有可用的论坛采集公司")

        industry_rows = stock_pairs(definition.get("industry"))
        eastmoney_sources = [*industry_rows[:1], *source_stocks]
        for code, name in eastmoney_sources:
            key = f"eastmoney:{code}"
            specs[key] = {
                "key": key,
                "platform": "eastmoney",
                "code": code,
                "name": f"{name}吧" if not str(name).endswith("吧") else name,
            }
            if key not in keys:
                keys.append(key)

        sina_stocks = stock_pairs(definition.get("sinaStocks")) or source_stocks[:1]
        for sina_code, sina_name in sina_stocks:
            sina_key = f"sina:{sina_code}"
            specs[sina_key] = {
                "key": sina_key,
                "platform": "sina",
                "code": sina_code,
                "name": sina_name,
            }
            if sina_key not in keys:
                keys.append(sina_key)

        ths_rows = definition.get("thsCommunities")
        if not isinstance(ths_rows, list) or not ths_rows:
            raise ValueError(f"板块 {sector_key} 没有配置同花顺板块社区")
        for community in ths_rows:
            if not isinstance(community, Mapping):
                continue
            ths_code = str(community.get("code") or "").strip()
            if not ths_code:
                continue
            ths_key = f"ths:sector:{sector_key}:{ths_code}"
            specs[ths_key] = {
                "key": ths_key,
                "platform": "ths",
                "code": ths_code,
                "name": str(community.get("name") or definition["name"]),
                "marketId": safe_int(community.get("marketId"), 48),
                "preferScene": True,
                "sectorKey": sector_key,
            }
            keys.append(ths_key)

        for taoguba_code, taoguba_name in source_stocks:
            taoguba_key = f"taoguba:{taoguba_code}"
            specs[taoguba_key] = {
                "key": taoguba_key,
                "platform": "taoguba",
                "code": taoguba_code,
                "name": taoguba_name,
                "sectorKey": sector_key,
            }
            if taoguba_key not in keys:
                keys.append(taoguba_key)
        sector_keys[sector_key] = keys
    return specs, market_keys, sector_keys


def fetch_source(spec: Mapping[str, object]) -> Dict[str, object]:
    platform = str(spec["platform"])
    if platform == "eastmoney":
        return fetch_eastmoney(spec)
    if platform == "sina":
        return fetch_sina(spec)
    if platform == "ths":
        return fetch_ths(spec)
    if platform == "taoguba":
        return fetch_taoguba(spec)
    raise RuntimeError(f"未知平台：{platform}")


def load_forum_cache() -> Dict[str, Dict[str, object]]:
    raw = load_json(FORUM_CACHE_PATH, {})
    if not isinstance(raw, dict):
        return {}
    restored: Dict[str, Dict[str, object]] = {}
    for old_key, source in raw.items():
        if not isinstance(source, dict):
            continue
        platform = str(source.get("platform") or "eastmoney")
        key = str(source.get("key") or old_key)
        if ":" not in key:
            key = f"{platform}:{key}"
        posts: List[Dict[str, object]] = []
        for item in source.get("posts", []):
            if not isinstance(item, dict):
                continue
            published = parse_time(item.get("publishTime"))
            if published is None:
                continue
            posts.append({
                **item,
                "published": published,
                "platform": str(item.get("platform") or platform),
                "platformName": str(item.get("platformName") or PLATFORM_NAMES.get(platform, platform)),
                "sourceKey": str(item.get("sourceKey") or key),
                "kind": str(item.get("kind") or "post"),
                # Legacy THS comments inherited the parent feed item's time.
                # Preserve that fact when an older cache predates the explicit
                # marker; other legacy posts used their own publication time.
                "timeInherited": bool(
                    item.get("timeInherited")
                    if "timeInherited" in item
                    else platform == "ths" and str(item.get("kind") or "post") == "comment"
                ),
            })
        metadata = collection_metadata(
            raw_times=[],
            posts=posts,
            raw_rows=safe_int(source.get("rawRows"), len(posts)),
            page_count=safe_int(source.get("pageCount"), 1 if posts else 0),
            cap_hit=source.get("capHit") if "capHit" in source else None,
            chronological=(
                bool(source.get("chronological"))
                if "chronological" in source
                else platform in {"eastmoney", "sina", "taoguba"}
            ),
            fresh_fetched_now=False,
            fetched_at=source.get("fetchedAt"),
        )
        # Raw-row coverage can include filtered posts. Retain saved evidence
        # when present, and only reconstruct fields for a legacy cache.
        for field in (
            "rawRows", "pageCount", "oldestSeen", "newestSeen", "capHit",
            "chronological", "coverageDays", "thirdTradingDayReached",
        ):
            if field in source:
                metadata[field] = source[field]
        metadata["freshFetchedNow"] = False
        metadata["rawRowsEstimated"] = bool(source.get("rawRowsEstimated", "rawRows" not in source))
        restored[key] = {
            **source,
            **metadata,
            "coverageStatus": str(source.get("coverageStatus") or "legacy_cache_unknown"),
            "key": key,
            "platform": platform,
            "posts": posts,
        }
    return restored


def save_forum_cache(results: Mapping[str, Dict[str, object]]) -> None:
    serializable: Dict[str, Dict[str, object]] = {}
    for key, source in results.items():
        posts = [{field: value for field, value in item.items() if field != "published"} for item in source.get("posts", [])]
        serializable[key] = {**source, "posts": posts}
    save_json(FORUM_CACHE_PATH, serializable)


def cache_is_usable(source: Mapping[str, object]) -> bool:
    fetched = parse_time(source.get("fetchedAt"))
    if fetched is None or NOW - fetched > timedelta(hours=72):
        return False
    return bool(source.get("posts"))


def cache_is_recent(source: Mapping[str, object], minutes: int = 45) -> bool:
    fetched = parse_time(source.get("fetchedAt"))
    return bool(source.get("posts")) and fetched is not None and NOW - fetched <= timedelta(minutes=minutes)


def accumulate_taoguba_snapshots(
    fresh: Dict[str, object],
    previous: Mapping[str, object] | None,
) -> Dict[str, object]:
    """Accumulate anonymous 20-row snapshots without claiming uncensored volume."""

    if str(fresh.get("platform") or "") != "taoguba" or not isinstance(previous, Mapping):
        return fresh
    combined: Dict[str, Dict[str, object]] = {}
    cutoff = NOW - timedelta(days=10)
    for source in (previous, fresh):
        for raw in source.get("posts", []):
            if not isinstance(raw, Mapping):
                continue
            post = dict(raw)
            published = post.get("published") if isinstance(post.get("published"), datetime) else parse_time(post.get("publishTime"))
            if published is None or published < cutoff:
                continue
            post["published"] = published
            identity = str(post.get("id") or hashlib.sha256(
                normalize_text(str(post.get("title") or "")).encode("utf-8")
            ).hexdigest())
            combined[identity] = post
    merged = sorted(combined.values(), key=lambda post: post["published"], reverse=True)
    return {
        **fresh,
        "posts": merged,
        "snapshotAccumulated": True,
        "snapshotAccumulatedPostCount": len(merged),
        "volumeIsLowerBound": True,
    }


def merge_window_evidence(
    current: Mapping[str, Dict[str, object]],
    archived: Mapping[str, Dict[str, object]],
    specs: Mapping[str, Dict[str, object]],
    as_of: datetime,
) -> Dict[str, Dict[str, object]]:
    """Reuse only already observed window posts; archive rows never prove zeroes."""
    days = set(scoring_trading_days(as_of))
    merged: Dict[str, Dict[str, object]] = {}
    for key in sorted(set(current) | (set(archived) & set(specs))):
        live = current.get(key)
        saved = archived.get(key, {})
        base = dict(live) if live is not None else {
            **specs[key], "posts": [], "fresh": False, "archive": True,
            "url": "", "chronological": False, "rawRowsEstimated": True,
            "coverageStatus": "archive_samples_only", "freshFetchedNow": False,
        }
        by_id: Dict[str, Dict[str, object]] = {}
        archive_ids: set[str] = set()
        skipped_quality = 0
        for is_archive, rows in ((True, saved.get("posts", [])), (False, base.get("posts", []))):
            for original in rows:
                post = dict(original)
                published = post.get("published")
                if not isinstance(published, datetime):
                    published = parse_time(post.get("publishTime"))
                if published is None or published.date() not in days or published > as_of:
                    continue
                if is_archive:
                    first_seen = parse_time(post.get("firstCollectedAt") or post.get("collectedAt"))
                    if first_seen is None or first_seen > as_of:
                        continue
                    # Old THS archives omitted the full-length/style evidence.
                    # Do not turn such an unknown long/promotional post into a short comment.
                    if str(base.get("platform")) == "ths" and "originalLength" not in post:
                        skipped_quality += 1
                        continue
                post["published"] = published
                post["sourceKey"] = key
                identity = str(post.get("id") or normalize_text(str(post.get("title") or "")))
                if is_archive:
                    archive_ids.add(identity)
                by_id[identity] = post  # current fields take precedence for the same id
        seen_text: set[str] = set()
        posts: List[Dict[str, object]] = []
        for post in sorted(by_id.values(), key=lambda row: (row["published"], str(row.get("id") or "")), reverse=True):
            text_key = normalize_text(str(post.get("title") or ""))
            if text_key and text_key in seen_text:
                continue
            seen_text.add(text_key)
            posts.append(post)
        posts, overflow = apply_author_daily_limit(posts)
        live_ids = {str(row.get("id") or normalize_text(str(row.get("title") or ""))) for row in base.get("posts", [])}
        reused = sum(str(row.get("id") or normalize_text(str(row.get("title") or ""))) in archive_ids - live_ids for row in posts)
        merged[key] = {
            **base, "posts": posts, "archiveReusedPosts": reused,
            "archiveQualityUnknownSkipped": skipped_quality, "mergedAuthorOverflow": overflow,
        }
    return merged


def market_window_diagnostics(
    results: Mapping[str, Dict[str, object]],
    market_keys: Sequence[str],
    daily_rows: Mapping[str, Mapping[str, Mapping[str, object]]],
    as_of: datetime,
) -> Dict[str, object]:
    days = scoring_trading_days(as_of)
    rows: List[Dict[str, object]] = []
    for key in market_keys:
        source = results.get(key, {})
        boundary = parse_time(source.get("chronologicalBoundary"))
        fetched = parse_time(source.get("fetchedAt"))
        verified = verified_zero_days_for_source(source, days, as_of)
        source_days = []
        for day in days:
            info = daily_rows.get(key, {}).get(day.isoformat(), {})
            sample_count = safe_int(info.get("sampleCount"))
            known = source.get("marketWindowPolicyVersion") == MARKET_WINDOW_POLICY_VERSION
            complete = bool(
                known and source.get("chronological") and fetched is not None
                and fetched >= datetime(day.year, day.month, day.day, 15, 10, tzinfo=CN_TZ)
                and (source.get("windowStartCovered") or (boundary is not None and boundary.date() < day))
                and source.get("paginationMode") != "html_single_page_fallback"
            )
            source_days.append({
                "date": day.isoformat(), "status": str(info.get("status") or "unobserved"),
                "sampleCount": sample_count, "streamCovered": complete,
                "verifiedZero": str(info.get("status") or "") == "verified_zero" and day.isoformat() in verified,
            })
        rows.append({
            "key": key, "platform": source.get("platform") or key.split(":", 1)[0],
            "name": source.get("name") or key, "fetchedAt": source.get("fetchedAt"),
            "freshFetchedNow": bool(source.get("freshFetchedNow")),
            "pageCount": safe_int(source.get("pageCount")), "pageLimit": source.get("pageLimit"),
            "coverageStatus": source.get("coverageStatus") or "unobserved",
            "thirdTradingDayReached": bool(source.get("thirdTradingDayReached")),
            "windowStartCovered": bool(source.get("windowStartCovered")),
            "chronologicalBoundary": source.get("chronologicalBoundary"),
            "archiveReusedPosts": safe_int(source.get("archiveReusedPosts")),
            "days": source_days,
        })
    return {
        "tradingDays": [day.isoformat() for day in days], "sources": rows,
        "missingChannels": [channel for channel in FAIR_CHANNELS if not any(
            row["platform"] == channel and any(day["status"] == "observed" for day in row["days"])
            for row in rows
        )],
        "note": "有样本不等于时序覆盖完整；档案补样不证明零帖，缺失席位仍保持中性。",
    }


def merge_posts(results: Mapping[str, Dict[str, object]], source_keys: Sequence[str]) -> List[Dict[str, object]]:
    merged: Dict[str, Dict[str, object]] = {}
    normalized_seen: set[str] = set()
    for source_key in source_keys:
        for original in results.get(source_key, {}).get("posts", []):
            post = dict(original)
            normalized = normalize_text(str(post.get("title") or ""))
            platform = str(post.get("platform") or "unknown")
            # 精确归一化正文跨平台、跨来源去重；平台只保留在 id 身份键中。
            content_key = normalized
            raw_id = str(post.get("id") or "")
            identity_key = raw_id if raw_id.startswith(f"{platform}:") else f"{platform}:{raw_id}"
            if identity_key in merged or (normalized and content_key in normalized_seen):
                continue
            merged[identity_key] = post
            if normalized:
                normalized_seen.add(content_key)
    return sorted(merged.values(), key=lambda item: item["published"], reverse=True)


def stable_estimated_post_key(post: Mapping[str, object]) -> bytes:
    """与情绪标签无关的历史稳定抽样键。

    正常档案都有平台帖子 id，因此正文改写或词典变化不会改变入样结果；只有
    极少数缺 id 的兼容数据才用归一化正文作为身份兜底。
    """

    post_id = str(post.get("id") or "")
    fallback = "" if post_id else normalize_text(str(post.get("title") or ""))
    identity = "|".join((
        str(post.get("sourceKey") or "unknown"),
        post_id,
        str(post.get("publishTime") or post.get("published") or ""),
        str(post.get("authorKey") or post.get("authorHash") or ""),
        fallback,
    ))
    return hashlib.sha256(identity.encode("utf-8")).digest()


def apply_author_daily_limit(
    visible: Sequence[Dict[str, object]],
) -> Tuple[List[Dict[str, object]], int]:
    """按作者×自然日限额，防止单一高产账号制造虚假热度。"""

    ordered = sorted(
        visible,
        key=lambda item: item.get("published") or datetime.min.replace(tzinfo=CN_TZ),
        reverse=True,
    )
    author_day_counts: Dict[Tuple[str, str], int] = defaultdict(int)
    author_limited: List[Dict[str, object]] = []
    author_overflow = 0
    for post in ordered:
        published = post.get("published")
        day = published.astimezone(CN_TZ).strftime("%Y-%m-%d") if isinstance(published, datetime) else "unknown"
        author = str(post.get("authorKey") or post.get("authorHash") or post.get("id") or normalize_text(str(post.get("title") or "")))
        bucket = (author, day)
        if author_day_counts[bucket] >= AUTHOR_DAILY_LIMIT:
            author_overflow += 1
            continue
        author_day_counts[bucket] += 1
        author_limited.append(post)

    return author_limited, author_overflow


def select_analysis_sample(
    visible: Sequence[Dict[str, object]],
    record_type: str,
) -> Tuple[List[Dict[str, object]], Dict[str, object]]:
    """执行作者限额；仅 estimated 的情绪比例再做来源×日期稳定限额。

    每来源每日 8 条只用于避免历史文本量不均匀地改变情绪词比例。讨论量、
    独立作者和发帖速度始终使用作者限额后的全部可见文本，不能与情绪抽样
    共用上限，否则真正热门的板块会被硬截成和普通板块一样热。
    """

    author_limited, author_overflow = apply_author_daily_limit(visible)

    if record_type == MEASURED:
        return author_limited, {
            "mode": MEASURED,
            "postsBefore": len(visible),
            "postsAfterAuthorLimit": len(author_limited),
            "postsSelected": len(author_limited),
            "authorOverflowRemoved": author_overflow,
            "sourceDayBucketsCapped": 0,
            "capPerSourcePublishedDay": None,
        }

    by_source_day: Dict[Tuple[str, str], List[Dict[str, object]]] = defaultdict(list)
    for post in author_limited:
        published = post.get("published")
        day = published.astimezone(CN_TZ).strftime("%Y-%m-%d") if isinstance(published, datetime) else "unknown"
        by_source_day[(str(post.get("sourceKey") or "unknown"), day)].append(post)
    selected: List[Dict[str, object]] = []
    capped_buckets = 0
    for bucket_posts in by_source_day.values():
        if len(bucket_posts) > ESTIMATED_SOURCE_DAY_LIMIT:
            capped_buckets += 1
        selected.extend(sorted(bucket_posts, key=stable_estimated_post_key)[:ESTIMATED_SOURCE_DAY_LIMIT])
    selected.sort(
        key=lambda item: item.get("published") or datetime.min.replace(tzinfo=CN_TZ),
        reverse=True,
    )
    return selected, {
        "mode": ESTIMATED,
        "postsBefore": len(visible),
        "postsAfterAuthorLimit": len(author_limited),
        "postsSelected": len(selected),
        "authorOverflowRemoved": author_overflow,
        "sourceDayBucketsCapped": capped_buckets,
        "capPerSourcePublishedDay": ESTIMATED_SOURCE_DAY_LIMIT,
        "selection": "sha256(sourceKey,id,publishTime,authorKey); title only when id missing",
    }


def source_activity_metrics(
    posts: Sequence[Mapping[str, object]],
    as_of: datetime,
    record_type: str,
) -> Dict[str, object]:
    """计算三交易日加权的社区发帖量与独立参与广度。

    最新、上一、上两个交易日的原始权重固定为 50%/30%/20%。若某个
    交易日没有真实可见样本，只在其余可用日之间归一化，不把缺日当成 0，
    更不会补入中性 50。周末和休市日文本不进入数量或速度口径。
    """

    window_days = scoring_trading_days(as_of)
    window_set = set(window_days)
    ordered = sorted(
        (
            post for post in posts
            if isinstance(post.get("published"), datetime)
            and post_age_days(post, as_of) >= 0.0
            and published_trading_day(post) in window_set
        ),
        key=lambda item: item["published"],
        reverse=True,
    )
    by_day: Dict[date, List[Mapping[str, object]]] = defaultdict(list)
    for post in ordered:
        day = published_trading_day(post)
        if day is not None:
            by_day[day].append(post)

    effective_weights = normalized_trading_day_weights(as_of, by_day)
    daily_rows: List[Dict[str, object]] = []
    weighted_daily_posts = 0.0
    weighted_author_ratio = 0.0
    all_authors: set[str] = set()
    for index, day in enumerate(window_days):
        day_posts = by_day.get(day, [])
        equivalent_posts = sum(
            (0.72 if post.get("kind") == "comment" else 1.0)
            * float(post.get("grassrootsStyleWeight") or grassroots_style_weight(post))
            for post in day_posts
        )
        authors = {
            str(post.get("authorKey") or post.get("authorHash") or post.get("id") or "unknown")
            for post in day_posts
        }
        all_authors.update(authors)
        author_ratio = len(authors) / len(day_posts) if day_posts else 0.0
        effective_weight = effective_weights.get(day, 0.0)
        weighted_daily_posts += effective_weight * equivalent_posts
        weighted_author_ratio += effective_weight * author_ratio
        daily_rows.append({
            "date": day.isoformat(),
            "rawWeight": TRADING_DAY_WEIGHTS[index],
            "effectiveWeight": round(effective_weight, 6),
            "postCount": len(day_posts),
            "equivalentPosts": round(equivalent_posts, 3),
            "uniqueAuthors": len(authors),
            "uniqueAuthorRatio": round(author_ratio, 4),
            "available": bool(day_posts),
        })

    anchor_posts = by_day.get(window_days[0], []) if window_days else []
    anchor_equivalent = sum(
        (0.72 if post.get("kind") == "comment" else 1.0)
        * float(post.get("grassrootsStyleWeight") or grassroots_style_weight(post))
        for post in anchor_posts
    )
    anchor_authors = {
        str(post.get("authorKey") or post.get("authorHash") or post.get("id") or "unknown")
        for post in anchor_posts
    }
    total_window_equivalent = sum(float(row["equivalentPosts"]) for row in daily_rows)
    posting_velocity = max(0.0, weighted_daily_posts)

    return {
        # 新口径字段。
        "method": "three_trading_day_weighted_daily_posts",
        "marketDate": window_days[0].isoformat() if window_days else None,
        "tradingDays": [day.isoformat() for day in window_days],
        "dayWeights": list(TRADING_DAY_WEIGHTS),
        "availableTradingDayCount": len(effective_weights),
        "missingTradingDaysExcluded": len(window_days) - len(effective_weights),
        "effectiveDayWeights": {
            day.isoformat(): round(weight, 6) for day, weight in effective_weights.items()
        },
        "dailyPostCounts": daily_rows,
        "weightedDailyPosts3d": round(weighted_daily_posts, 3),
        "totalEquivalentPosts3d": round(total_window_equivalent, 3),
        "uniqueAuthors3d": len(all_authors),
        "uniqueAuthorRatio3d": round(weighted_author_ratio, 4),
        "calendarMode": calendar_mode(window_days[0]) if window_days else calendar_mode(as_of),
        # 兼容既有页面和板块热度模型的旧字段；数值已切换到三交易日口径。
        "recent24Equivalent": round(anchor_equivalent, 3),
        "effective7Equivalent": round(total_window_equivalent, 3),
        "dailyEquivalent": round(weighted_daily_posts, 3),
        "uniqueAuthors24": len(anchor_authors),
        "uniqueAuthorRatio24": round(weighted_author_ratio, 4),
        "postingVelocity": round(posting_velocity, 3),
        "logPostingVelocity": round(math.log1p(posting_velocity), 6),
        "spanRate": None,
        "spanHours": None,
        "spanSamplePosts": 0,
        "lowerBound": False,
        "usesInteractions": False,
        "usesMarketPrice": False,
    }


def estimated_attention_score(
    posts: Sequence[Mapping[str, object]],
    as_of: datetime,
) -> Tuple[float, Dict[str, object]]:
    """Compatibility helper for three-trading-day retrospective attention."""

    metrics = source_activity_metrics(posts, as_of, ESTIMATED)
    daily_equivalent = float(metrics["weightedDailyPosts3d"])
    activity = round1(
        50.0 + 30.0 * math.tanh(math.log((daily_equivalent + 2.0) / 10.0) / 1.3)
    )
    return activity, {
        "method": "three_trading_day_weighted_post_volume",
        "status": "applied" if metrics["availableTradingDayCount"] else "unavailable",
        "reason": None if metrics["availableTradingDayCount"] else "no_available_trading_day_samples",
        "dailyEquivalent": round(daily_equivalent, 3),
        "weightedDailyPosts3d": round(daily_equivalent, 3),
        "tradingDays": metrics["tradingDays"],
        "effectiveDayWeights": metrics["effectiveDayWeights"],
        "missingDaysAssumedZero": False,
        "usesFutureDates": False,
    }


def classify_persona_evidence(text: str) -> Tuple[str | None, str | None, str]:
    """互斥识别行为语料，并返回遮住分群词后的评分文本。"""

    cleaned = str(text or "").strip()
    label: str | None = None
    reason: str | None = None
    patterns: Tuple[re.Pattern[str], ...] = ()
    if HOUSEHOLD_EXPLICIT_RE.search(cleaned):
        label, reason = "baoma", "explicit_household"
        patterns = (HOUSEHOLD_EXPLICIT_RE, HOUSEHOLD_CAUTIOUS_RE)
    elif HOUSEHOLD_CAUTIOUS_RE.search(cleaned):
        label, reason = "baoma", "cautious_household_style"
        patterns = (HOUSEHOLD_EXPLICIT_RE, HOUSEHOLD_CAUTIOUS_RE)
    elif NOVICE_EVIDENCE_RE.search(cleaned) or (
        re.search(r"[?？]", cleaned) and NOVICE_MARKET_CONTEXT_RE.search(cleaned)
    ):
        label = "novice"
        reason = "explicit_question_or_learning" if NOVICE_EVIDENCE_RE.search(cleaned) else "market_question"
        patterns = (NOVICE_EVIDENCE_RE,)
    elif RETAIL_ACTION_RE.search(cleaned):
        label, reason = "retail", "active_trading_language"
        patterns = (RETAIL_ACTION_RE,)
    masked = cleaned
    for pattern in patterns:
        masked = pattern.sub(" ", masked)
    masked = re.sub(r"\s+", " ", masked).strip(" ，。！？!?：:；;")
    return label, reason, masked


def _topic_terms(topic: object = None, source: object = None) -> List[str]:
    values: List[object] = [topic]
    if isinstance(source, Mapping):
        values.extend((source.get("name"), source.get("source"), source.get("topic")))
    else:
        values.append(source)
    generic = {"公开社区", "东方财富股吧", "新浪股吧", "同花顺论股堂", "股吧", "行业吧"}
    terms: List[str] = []
    for value in values:
        term = str(value or "").strip()
        for suffix in ("东方财富股吧", "新浪股吧", "同花顺论股堂", "行业吧", "股吧", "吧"):
            if term.endswith(suffix):
                term = term[:-len(suffix)].strip()
                break
        if len(term) >= 2 and term not in generic and term not in terms:
            terms.append(term)
            for company_suffix in ("股份有限公司", "股份", "集团"):
                if term.endswith(company_suffix) and len(term) - len(company_suffix) >= 2:
                    alias = term[:-len(company_suffix)]
                    if alias not in terms:
                        terms.append(alias)
    return terms


def trade_switch_adjustment(text: str, topic: object = None, source: object = None) -> int:
    """识别从当前标的换到别的标的；有 topic/source 时判断换仓方向。"""

    compact = re.sub(r"\s+", "", str(text or ""))
    generic_away = re.search(
        r"(?:卖出|清仓|减仓)(?:当前|这只|本股|手中|持有的)?(?:标的|股票|个股|票)?"
        r".{0,10}(?:买入|换入)(?:别的|其他|其它|另一只|另一个)",
        compact,
    )
    if generic_away:
        return -3
    for term in sorted(_topic_terms(topic, source), key=len, reverse=True):
        escaped = re.escape(term)
        sell = re.search(rf"(?:卖出|清仓|减仓).{{0,8}}{escaped}", compact)
        buy = re.search(rf"(?:买入|加仓|换入).{{0,8}}{escaped}", compact)
        if sell and re.search(r"(?:买入|换入)", compact[sell.end():]):
            return -3
        if buy and re.search(r"(?:卖出|清仓|减仓)", compact[:buy.start()]):
            return 2
    return 0


def dual_directional_condition(text: str) -> bool:
    compact = re.sub(r"\s+", "", str(text or ""))
    bullish = re.search(r"(?:突破|站上|上破|上涨).{0,12}(?:买入|加仓|持有|看多|做多)", compact)
    bearish = re.search(r"(?:跌破|破位|下跌).{0,12}(?:卖出|减仓|清仓|止损|看空)", compact)
    bearish_scenario = re.search(r"(?:如果|假如|若|要是).{0,12}(?:下探|回落|跌破|破位|下跌)", compact)
    return bool(
        bullish and (bearish or bearish_scenario)
        and re.search(r"如果|假如|若|要是|就|则|否则", compact)
    )


def has_negating_prefix(prefix: str) -> bool:
    """只在当前分句的紧邻前缀中识别否定，避免跨逗号误反转下一分句。"""

    clause = re.split(r"[，,。！？!?；;：:]", prefix)[-1]
    nearby = clause[-5:]
    if nearby.endswith(("看不出", "没看出", "看不出来", "没看出来")):
        return False
    return any(word in nearby for word in NEGATION_WORDS)


def sentiment_score(text: str, topic: object = None, source: object = None) -> int:
    """透明的短语情绪分；每个短语每帖最多贡献一次。

    否定前缀会反转短语方向，紧邻的反讽/质疑后缀会反转正向词；同时出现
    正负证据时适度收缩，避免“龙头……天天跌”被单一标签词推成强看多。
    """

    working = str(text or "")
    positive_total = 0.0
    negative_total = 0.0
    entries = sorted([*POSITIVE.items(), *NEGATIVE.items()], key=lambda item: len(item[0]), reverse=True)
    for phrase, base_weight in entries:
        index = working.find(phrase)
        if index < 0:
            continue
        prefix = working[max(0, index - 5):index]
        suffix = working[index + len(phrase):index + len(phrase) + 9]
        weight = float(base_weight)
        if has_negating_prefix(prefix):
            weight = -weight
        elif weight > 0 and (
            any(suffix.startswith(marker) for marker in NEGATION_SUFFIXES)
            or any(marker in prefix for marker in CAUTION_PREFIXES)
        ):
            weight = -abs(weight)
        elif weight > 0 and any(marker in suffix or marker in prefix for marker in SKEPTICAL_MARKERS):
            weight = -abs(weight)
        if weight > 0:
            positive_total += weight
        elif weight < 0:
            negative_total += weight
        # 全部替换可避免短语重复刷分，也避免“暴涨”再命中“上涨”等重叠词。
        working = working.replace(phrase, " ")

    switch = trade_switch_adjustment(text, topic=topic, source=source)
    if switch > 0:
        positive_total += switch
    elif switch < 0:
        negative_total += switch
    total = positive_total + negative_total
    if positive_total and negative_total:
        total *= 0.80
    if dual_directional_condition(text):
        # “突破就买、跌破就卖”是交易计划，不是已经形成的单边方向判断。
        total *= 0.20
    if any(marker in text for marker in UNCERTAINTY_MARKERS):
        total *= 0.40
    return max(-6, min(6, int(round(total))))


def contains_any(text: str, words: Iterable[str]) -> bool:
    return any(word in text for word in words)


def contains_nonnegated(text: str, words: Iterable[str]) -> bool:
    for word in words:
        start = 0
        while True:
            index = text.find(word, start)
            if index < 0:
                break
            prefix = text[max(0, index - 5):index]
            suffix = text[index + len(word):index + len(word) + 9]
            if (
                not has_negating_prefix(prefix)
                and not any(suffix.startswith(marker) for marker in NEGATION_SUFFIXES)
                and not any(marker in prefix for marker in CAUTION_PREFIXES)
                and not any(marker in suffix for marker in SKEPTICAL_MARKERS)
            ):
                return True
            start = index + len(word)
    return False


def newcomer_flag(text: str) -> bool:
    if contains_any(text, NEWCOMER_WORDS):
        return True
    return bool(re.search(r"[?？]", text) and contains_any(text, QUESTION_WORDS))


def engagement_weight(post: Mapping[str, object], *, use_interactions: bool = True) -> float:
    comments = max(0.0, float(post.get("comments") or 0)) if use_interactions else 0.0
    likes = max(0.0, float(post.get("likes") or 0)) if use_interactions else 0.0
    interaction = min(math.log1p(comments) * 0.13 + math.log1p(likes) * 0.08, 0.65)
    kind_weight = 0.72 if post.get("kind") == "comment" else 1.0
    return kind_weight * (1.0 + interaction)


def post_age_days(post: Mapping[str, object], as_of: datetime = NOW) -> float:
    published = post.get("published")
    if not isinstance(published, datetime):
        return float(LOOKBACK_DAYS)
    return (as_of - published).total_seconds() / 86400.0


def scoring_market_day(as_of: datetime = NOW) -> date:
    """Return the A-share session that owns a scoring snapshot.

    A weekend or exchange holiday update belongs to the latest completed
    trading date.  On a trading day, an update before 15:10 is also assigned
    to the previous completed session: otherwise a partial morning/afternoon
    post count would receive the same 50% weight as a full trading day.
    ``updatedAt`` still records the real collection time; this helper only
    controls the scoring window and the history-point date.
    """

    local = as_of.replace(tzinfo=CN_TZ) if as_of.tzinfo is None else as_of.astimezone(CN_TZ)
    if is_trading_day(local.date()) and (local.hour, local.minute) < (15, 10):
        return latest_trading_day(local.date() - timedelta(days=1))
    return latest_trading_day(local)


def scoring_trading_days(as_of: datetime = NOW) -> List[date]:
    """Return the three scoring sessions, newest first."""

    return previous_trading_days(scoring_market_day(as_of), TRADING_WINDOW_SIZE)


def published_trading_day(post: Mapping[str, object]) -> date | None:
    published = post.get("published")
    if not isinstance(published, datetime):
        return None
    local = published.replace(tzinfo=CN_TZ) if published.tzinfo is None else published.astimezone(CN_TZ)
    return local.date()


def normalized_trading_day_weights(
    as_of: datetime,
    available_days: Iterable[date],
) -> Dict[date, float]:
    """Normalise 50/30/20 only over trading days that contain real samples."""

    available = set(available_days)
    raw = {
        day: TRADING_DAY_WEIGHTS[index]
        for index, day in enumerate(scoring_trading_days(as_of))
        if day in available
    }
    total = sum(raw.values())
    if total <= 0:
        return {}
    return {day: weight / total for day, weight in raw.items()}


def trading_day_gap(previous: date, current: date) -> int:
    """Count A-share sessions after ``previous`` through ``current``."""

    if current <= previous:
        return 1
    cursor = previous + timedelta(days=1)
    count = 0
    while cursor <= current:
        if is_trading_day(cursor):
            count += 1
        cursor += timedelta(days=1)
    return max(1, count)


def signal_from_rate(rate: float, kind: str) -> float:
    if kind == "fomo":
        return round1(50.0 + 30.0 * math.tanh((rate - 0.06) / 0.07))
    if kind == "newcomer":
        return round1(50.0 + 28.0 * math.tanh((rate - 0.02) / 0.035))
    if kind == "intensity":
        return round1(50.0 + 25.0 * math.tanh((rate - 0.18) / 0.16))
    raise ValueError(kind)


def source_analysis(
    posts: Sequence[Dict[str, object]],
    as_of: datetime = NOW,
    record_type: str = MEASURED,
) -> Dict[str, object] | None:
    window_days = scoring_trading_days(as_of)
    window_set = set(window_days)
    eligible: List[Dict[str, object]] = []
    for original in posts:
        if (
            not valid_text(str(original.get("title") or ""))
            or not 0.0 <= post_age_days(original, as_of) <= LOOKBACK_DAYS
            or published_trading_day(original) is None
        ):
            continue
        post = dict(original)
        post["grassrootsStyleWeight"] = grassroots_style_weight(post)
        if float(post["grassrootsStyleWeight"]) <= 0.0:
            continue
        eligible.append(post)
    visible = [
        post for post in eligible
        if published_trading_day(post) in window_set
    ]
    non_trading_excluded = sum(
        1 for post in eligible
        if not is_trading_day(published_trading_day(post))
    )
    # 回填档案先限制单一作者，再在 estimated 中按来源×发布日期做稳定限额；
    # measured 保持原有选样路径不变。
    activity_sample, _ = apply_author_daily_limit(visible)
    sample, sampling_policy = select_analysis_sample(visible, record_type)
    if not sample:
        return None
    activity_metrics = source_activity_metrics(activity_sample, as_of, record_type)
    annotated: List[Dict[str, object]] = []
    for post in sample:
        text = str(post.get("title") or "")
        topic = post.get("topic") or post.get("sourceCode")
        source = post.get("source")
        raw = sentiment_score(text, topic=topic, source=source)
        persona, evidence_reason, masked_text = classify_persona_evidence(text)
        masked_raw = sentiment_score(masked_text, topic=topic, source=source) if masked_text else 0
        annotated.append({
            **post,
            "rawScore": raw,
            "personaEvidence": persona,
            "personaEvidenceReason": evidence_reason,
            "maskedEvidenceText": masked_text,
            "maskedRawScore": masked_raw,
            "label": "看多" if raw > 0 else "看空" if raw < 0 else "中性",
            "fomoFlag": contains_nonnegated(text, FOMO_WORDS),
            "panicFlag": contains_nonnegated(text, PANIC_WORDS),
            "newcomerFlag": newcomer_flag(text),
            "intenseFlag": abs(raw) >= 2 or contains_any(text, INTENSE_WORDS) or len(re.findall(r"[!！?？]", text)) >= 2,
            "tradingDay": published_trading_day(post).isoformat() if published_trading_day(post) else None,
        })
    annotated_by_day: Dict[date, List[Dict[str, object]]] = defaultdict(list)
    for post in annotated:
        day = published_trading_day(post)
        if day is not None:
            annotated_by_day[day].append(post)
    effective_day_weights = normalized_trading_day_weights(as_of, annotated_by_day)
    daily_values: Dict[date, Dict[str, float]] = {}
    for day, day_posts in annotated_by_day.items():
        totals = defaultdict(float)
        denominator = 0.0
        for post in day_posts:
            # 正式情绪只看文本本身。同一段话不能因为某个平台累计了更多点赞/
            # 评论就获得更大方向权重，否则 measured 与历史 estimated 会形成
            # 不可比较的两套口径，也会把平台推荐算法混进社区情绪。
            weight = (
                engagement_weight(post, use_interactions=False)
                * float(post.get("grassrootsStyleWeight") or 0.0)
            )
            denominator += weight
            raw = int(post["rawScore"])
            totals["net"] += weight * math.tanh(raw / 2.2)
            for key in ("fomo", "panic", "newcomer", "intense"):
                totals[key] += weight if post[f"{key}Flag"] else 0.0
        if denominator > 0:
            daily_values[day] = {
                key: value / denominator for key, value in totals.items()
            }
    if not daily_values or not effective_day_weights:
        return None
    raw_metrics = {
        key: sum(
            effective_day_weights.get(day, 0.0) * values.get(key, 0.0)
            for day, values in daily_values.items()
        )
        for key in ("net", "fomo", "panic", "newcomer", "intense")
    }

    sampling_policy = {
        **sampling_policy,
        "scoringWindow": "latest_three_a_share_trading_days",
        "marketDate": window_days[0].isoformat(),
        "tradingDays": [day.isoformat() for day in window_days],
        "rawDayWeights": {
            day.isoformat(): TRADING_DAY_WEIGHTS[index]
            for index, day in enumerate(window_days)
        },
        "effectiveDayWeights": {
            day.isoformat(): round(weight, 6)
            for day, weight in effective_day_weights.items()
        },
        "availableTradingDayCount": len(effective_day_weights),
        "missingTradingDaysExcluded": len(window_days) - len(effective_day_weights),
        "nonTradingPostsExcluded": non_trading_excluded,
        "missingDaysAssumedNeutral": False,
    }

    median_comments = statistics.median([safe_int(post.get("comments")) for post in annotated]) if record_type == MEASURED else 0.0
    daily_equivalent = float(activity_metrics["weightedDailyPosts3d"])
    activity = 50.0 + 30.0 * math.tanh(math.log((daily_equivalent + 2.0) / 10.0) / 1.3)
    interaction = (
        50.0 + 20.0 * math.tanh(math.log1p(median_comments) / 3.0)
        if record_type == MEASURED else None
    )
    # 互动只保留为解释字段，不进入正式 attention。这样当前实测与历史回溯
    # 使用完全相同的文本/发帖量定义，并避免不同平台的推荐机制改变指数。
    attention = round1(activity)
    attention_baseline = {
        "method": "three_trading_day_weighted_post_volume",
        "status": "applied",
        "dailyEquivalent": round(daily_equivalent, 3),
        "weightedDailyPosts3d": round(daily_equivalent, 3),
        "activityScore": round1(activity),
        "interactionScore": round1(interaction) if interaction is not None else None,
        "volumeWeight": 1.0,
        "interactionWeight": 0.0,
        "interactionUsedForScoring": False,
        "tradingDays": [day.isoformat() for day in window_days],
        "effectiveDayWeights": activity_metrics["effectiveDayWeights"],
        "missingDaysAssumedZero": False,
    }
    signals = {
        "optimism": round1(50.0 + 45.0 * raw_metrics["net"]),
        "fomo": signal_from_rate(raw_metrics["fomo"], "fomo"),
        "panic": round1(100.0 * (1.0 - math.exp(-raw_metrics["panic"] / 0.12))),
        "newcomer": signal_from_rate(raw_metrics["newcomer"], "newcomer"),
        "intensity": signal_from_rate(raw_metrics["intense"], "intensity"),
        "attention": attention,
    }
    signals["heat"] = round1(
        signals["fomo"] * 0.30 + signals["newcomer"] * 0.15
        + signals["intensity"] * 0.25 + signals["attention"] * 0.30
    )
    return {
        "signals": signals,
        "raw": raw_metrics,
        "dailyRaw": [
            {
                "date": day.isoformat(),
                "rawWeight": TRADING_DAY_WEIGHTS[index],
                "effectiveWeight": round(effective_day_weights.get(day, 0.0), 6),
                "sampleCount": max(0, int(round(sum(
                    float(post.get("grassrootsStyleWeight") or 0.0)
                    for post in annotated_by_day.get(day, [])
                )))),
                "rawSampleCount": len(annotated_by_day.get(day, [])),
                "available": day in daily_values,
                "metrics": {
                    key: round(float(daily_values.get(day, {}).get(key, 0.0)), 6)
                    for key in ("net", "fomo", "panic", "newcomer", "intense")
                } if day in daily_values else None,
            }
            for index, day in enumerate(window_days)
        ],
        "posts": annotated,
        "activity": activity_metrics,
        "sampleCount": max(0, int(round(sum(
            float(post.get("grassrootsStyleWeight") or 0.0) for post in annotated
        )))),
        "rawSampleCount": len(annotated),
        "grassrootsEffectiveSample": round(sum(
            float(post.get("grassrootsStyleWeight") or 0.0) for post in annotated
        ), 3),
        "grassrootsFullWeightShare": round(
            sum(float(post.get("grassrootsStyleWeight") or 0.0) >= 0.95 for post in annotated)
            / max(1, len(annotated)),
            4,
        ),
        "activityPostCount": len(activity_sample),
        # 旧字段继续供页面使用：recent24 表示归属交易日，recent7 表示三交易日窗口。
        "recent24": sum(1 for post in activity_sample if published_trading_day(post) == window_days[0]),
        "recent7": len(activity_sample),
        "tradingWindowPosts": len(activity_sample),
        "currentTradingDayPosts": sum(
            1 for post in activity_sample if published_trading_day(post) == window_days[0]
        ),
        "tradingWindow": {
            "marketDate": window_days[0].isoformat(),
            "tradingDays": [day.isoformat() for day in window_days],
            "rawDayWeights": list(TRADING_DAY_WEIGHTS),
            "effectiveDayWeights": {
                day.isoformat(): round(weight, 6)
                for day, weight in effective_day_weights.items()
            },
            "availableTradingDayCount": len(effective_day_weights),
            "missingTradingDaysExcluded": len(window_days) - len(effective_day_weights),
            "nonTradingPostsExcluded": non_trading_excluded,
        },
        "medianComments": float(median_comments),
        "bullish": sum(1 for post in annotated if post["label"] == "看多"),
        "bearish": sum(1 for post in annotated if post["label"] == "看空"),
        "neutral": sum(1 for post in annotated if post["label"] == "中性"),
        "samplingPolicy": sampling_policy,
        "attentionBaseline": attention_baseline,
    }


def persona_evidence_diagnostics(
    analyses: Sequence[Mapping[str, object]],
    baseline_direction: float,
    as_of: datetime,
) -> Dict[str, object]:
    """汇总互斥行为证据，产出不参与正式指数的遮蔽词候选方向分。"""

    pool: List[Mapping[str, object]] = []
    for analysis in analyses:
        pool.extend(
            post for post in analysis.get("posts", [])
            if isinstance(post, Mapping) and post.get("personaEvidence") in PERSONA_EVIDENCE_KEYS
        )
    counts = Counter(str(post.get("personaEvidence")) for post in pool)
    reason_counts: Dict[str, Counter[str]] = defaultdict(Counter)
    for post in pool:
        reason_counts[str(post.get("personaEvidence"))][str(post.get("personaEvidenceReason"))] += 1

    candidates: Dict[str, Dict[str, object]] = {}
    for persona in PERSONA_EVIDENCE_KEYS:
        cohort = [post for post in pool if post.get("personaEvidence") == persona]
        sources = {str(post.get("sourceKey") or "unknown") for post in cohort}
        platforms = {str(post.get("platform") or "unknown") for post in cohort}
        if not cohort:
            candidates[persona] = {
                "available": False,
                "evidencePosts": 0,
                "sourceCount": 0,
                "platformCount": 0,
                "maskedDirectionRaw": None,
                "evidenceWeight": 0.0,
                "candidateShrunkDirection": None,
            }
            continue
        weighted_net = 0.0
        denominator = 0.0
        for post in cohort:
            weight = (0.72 if post.get("kind") == "comment" else 1.0) * (
                0.5 ** (post_age_days(post, as_of) / 7.0)
            )
            denominator += weight
            weighted_net += weight * math.tanh(safe_int(post.get("maskedRawScore")) / 2.2)
        raw_direction = round1(50.0 + 45.0 * weighted_net / max(denominator, 1e-9))
        sample_ratio = min(1.0, len(cohort) / 20.0)
        source_ratio = min(1.0, len(sources) / 3.0)
        platform_ratio = min(1.0, len(platforms) / 2.0)
        evidence_weight = min(0.65, math.sqrt(sample_ratio * source_ratio * platform_ratio))
        candidate = baseline_direction + evidence_weight * (raw_direction - baseline_direction)
        candidates[persona] = {
            "available": True,
            "evidencePosts": len(cohort),
            "sourceCount": len(sources),
            "platformCount": len(platforms),
            "maskedDirectionRaw": raw_direction,
            "evidenceWeight": round(evidence_weight, 3),
            "candidateShrunkDirection": round1(candidate),
        }
    return {
        "status": "experimental_not_used_in_formal_scores",
        "formalScoresUnchanged": True,
        "interpretation": {
            "baoma": "家庭/生活资金或稳健长期型表达，不推断真实身份",
            "novice": "明确提问、求教或首次入市型表达",
            "retail": "明确买卖、仓位或主动交易型表达",
        },
        "exclusivePriority": list(PERSONA_EVIDENCE_KEYS),
        "maskedBeforeCandidateScoring": True,
        "evidenceCounts": {key: int(counts.get(key, 0)) for key in PERSONA_EVIDENCE_KEYS},
        "reasonCounts": {
            key: dict(reason_counts.get(key, Counter())) for key in PERSONA_EVIDENCE_KEYS
        },
        "candidates": candidates,
    }


def build_platform_calibration(
    results: Mapping[str, Dict[str, object]],
    as_of: datetime = NOW,
    record_type: str = MEASURED,
) -> Dict[str, Dict[str, List[float]]]:
    """建立当日的平台内参照分布，修正页面容量和用户语言的结构差异。"""
    calibration: Dict[str, Dict[str, List[float]]] = defaultdict(lambda: defaultdict(list))
    for source in results.values():
        analysis = source_analysis(source.get("posts", []), as_of, record_type)
        if not analysis:
            continue
        platform = str(source.get("platform") or "unknown")
        for key in SIGNAL_KEYS:
            calibration[platform][key].append(float(analysis["signals"][key]))
        activity = analysis.get("activity") if isinstance(analysis.get("activity"), Mapping) else {}
        calibration[platform]["activityVelocity"].append(
            float(activity.get("logPostingVelocity") or 0.0)
        )
        calibration[platform]["activityBreadth"].append(
            float(activity.get("uniqueAuthorRatio3d") or activity.get("uniqueAuthorRatio24") or 0.0)
        )
    return {
        platform: {key: sorted(values) for key, values in signals.items()}
        for platform, signals in calibration.items()
    }


def percentile_score(
    values: Sequence[float],
    value: float,
    low: float = 20.0,
    high: float = 80.0,
) -> float:
    if len(values) < 8:
        return 50.0
    lower = sum(1 for candidate in values if candidate < value)
    equal = sum(1 for candidate in values if candidate == value)
    percentile = (lower + 0.5 * max(equal, 1)) / len(values)
    # 平台内最冷/最热不触碰 0/100，避免单日横截面制造虚假极值。
    return low + (high - low) * percentile


def calibrate_source_analysis(
    analysis: Dict[str, object],
    platform: str,
    calibration: Mapping[str, Mapping[str, Sequence[float]]],
) -> Dict[str, object]:
    signals = dict(analysis["signals"])
    platform_reference = calibration.get(platform, {})
    for key in SIGNAL_KEYS:
        absolute = float(signals[key])
        reference = platform_reference.get(key, [])
        if len(reference) < 8:
            continue
        relative = percentile_score(reference, absolute)
        absolute_weight = ABSOLUTE_SIGNAL_WEIGHT[key]
        signals[key] = round1(absolute * absolute_weight + relative * (1.0 - absolute_weight))
    activity = dict(analysis.get("activity") or {})
    activity_reference = platform_reference.get("activityVelocity", [])
    breadth_reference = platform_reference.get("activityBreadth", [])
    if len(activity_reference) >= 8:
        velocity_score = percentile_score(
            activity_reference,
            float(activity.get("logPostingVelocity") or 0.0),
            low=15.0,
            high=85.0,
        )
        breadth_score = (
            percentile_score(
                breadth_reference,
                float(activity.get("uniqueAuthorRatio3d") or activity.get("uniqueAuthorRatio24") or 0.0),
                low=25.0,
                high=75.0,
            )
            if len(breadth_reference) >= 8 else 50.0
        )
        activity["velocityScore"] = round1(velocity_score)
        activity["breadthScore"] = round1(breadth_score)
        activity["relativeScore"] = round1(
            ACTIVITY_VELOCITY_WEIGHT * velocity_score
            + ACTIVITY_BREADTH_WEIGHT * breadth_score
        )
    else:
        activity["relativeScore"] = round1(signals.get("attention", 50.0))
        activity["velocityScore"] = activity["relativeScore"]
        activity["breadthScore"] = 50.0
    activity["platformReferenceCount"] = len(activity_reference)
    signals["heat"] = round1(
        signals["fomo"] * 0.30 + signals["newcomer"] * 0.15
        + signals["intensity"] * 0.25 + signals["attention"] * 0.30
    )
    return {**analysis, "signals": signals, "activity": activity}


FAIR_SIGNAL_KEYS = ("optimism", "fomo", "panic", "newcomer", "intensity", "attention")
FAIR_SIGNAL_BASELINES = {
    "optimism": 50.0,
    "fomo": 50.0,
    "panic": 0.0,
    "newcomer": 50.0,
    "intensity": 50.0,
    "attention": 50.0,
}


def _daily_absolute_signals(metrics: Mapping[str, object]) -> Dict[str, float]:
    """Convert one platform/day's text rates to the public six-signal scale."""

    net = float(metrics.get("net") or 0.0)
    fomo = max(0.0, float(metrics.get("fomo") or 0.0))
    panic = max(0.0, float(metrics.get("panic") or 0.0))
    newcomer = max(0.0, float(metrics.get("newcomer") or 0.0))
    intense = max(0.0, float(metrics.get("intense") or 0.0))
    return {
        "optimism": round1(50.0 + 45.0 * net),
        "fomo": signal_from_rate(fomo, "fomo"),
        "panic": round1(100.0 * (1.0 - math.exp(-panic / 0.12))),
        "newcomer": signal_from_rate(newcomer, "newcomer"),
        "intensity": signal_from_rate(intense, "intensity"),
    }


def verified_zero_days_for_source(
    source: Mapping[str, object],
    trading_days: Sequence[date],
    as_of: datetime,
) -> set[str]:
    """Return only days for which a chronological collector proves absence.

    A missing row is not a zero.  We require a head-to-boundary chronological
    read, a fetch made after the completed scoring session, and a raw timestamp
    older than the candidate day.  THS hot_feed is intentionally never eligible.
    """

    if str(source.get("platform") or "") == "ths" or not bool(source.get("chronological")):
        return set()
    if bool(source.get("rawRowsEstimated")):
        return set()
    coverage_status = str(source.get("coverageStatus") or "")
    coverage_proved = (
        bool(source.get("chronologicalBoundary"))
        if source.get("marketWindowPolicyVersion") is not None
        else bool(source.get("thirdTradingDayReached"))
    ) or coverage_status in {
        "third_trading_day_reached",
        "source_exhausted_before_third_trading_day",
    }
    if not coverage_proved:
        return set()
    # New market collectors expose a conservative chronological tail boundary.
    # A stale pinned post in oldestSeen must never prove a missing day is zero.
    if source.get("marketWindowPolicyVersion") is not None:
        coverage_proved = bool(source.get("chronologicalBoundary")) or bool(source.get("windowStartCovered"))
        if not coverage_proved:
            return set()
        oldest_value = source.get("chronologicalBoundary")
    else:
        oldest_value = source.get("oldestSeen")
    oldest = parse_time(oldest_value)
    fetched = parse_time(source.get("fetchedAt"))
    if oldest is None or fetched is None:
        return set()
    market_day = scoring_market_day(as_of)
    session_complete = datetime(
        market_day.year,
        market_day.month,
        market_day.day,
        15,
        10,
        tzinfo=CN_TZ,
    )
    if fetched < session_complete:
        return set()
    return {
        day.isoformat()
        for day in trading_days
        if oldest.date() < day <= market_day
    }


def build_daily_calibrated_source_rows(
    results: Mapping[str, Dict[str, object]],
    analyses: Mapping[str, Mapping[str, object] | None],
    trading_days: Sequence[date],
    as_of: datetime,
) -> Dict[str, Dict[str, Dict[str, object]]]:
    """Calibrate each source inside the same platform and the same trading day.

    Emotion keeps its absolute textual meaning (70%-75%) and uses the platform
    percentile only as a capacity/language correction.  Attention is purely a
    same-platform daily percentile, so a 100-row page cannot beat a 50-row page
    merely because the website exposes more rows.
    """

    base_rows: Dict[str, Dict[str, Dict[str, object]]] = {}
    references: Dict[str, Dict[str, Dict[str, List[float]]]] = defaultdict(
        lambda: defaultdict(lambda: defaultdict(list))
    )
    for source_key, source in results.items():
        platform = str(source.get("platform") or "unknown")
        analysis = analyses.get(source_key)
        raw_rows = analysis.get("dailyRaw", []) if isinstance(analysis, Mapping) else []
        raw_by_day = {
            str(row.get("date")): row
            for row in raw_rows
            if isinstance(row, Mapping) and row.get("date")
        }
        activity = analysis.get("activity", {}) if isinstance(analysis, Mapping) else {}
        activity_rows = activity.get("dailyPostCounts", []) if isinstance(activity, Mapping) else []
        activity_by_day = {
            str(row.get("date")): row
            for row in activity_rows
            if isinstance(row, Mapping) and row.get("date")
        }
        verified_zeros = verified_zero_days_for_source(source, trading_days, as_of)
        source_days: Dict[str, Dict[str, object]] = {}
        for day in trading_days:
            day_key = day.isoformat()
            raw_row = raw_by_day.get(day_key)
            activity_row = activity_by_day.get(day_key, {})
            if isinstance(raw_row, Mapping) and bool(raw_row.get("available")):
                raw_metrics = raw_row.get("metrics") if isinstance(raw_row.get("metrics"), Mapping) else {}
                signals = _daily_absolute_signals(raw_metrics)
                volume = max(
                    0.0,
                    float(activity_row.get("equivalentPosts") or activity_row.get("postCount") or 0.0),
                )
                post_count = max(0, safe_int(activity_row.get("postCount"), safe_int(raw_row.get("sampleCount"))))
                unique_authors = max(0, safe_int(activity_row.get("uniqueAuthors")))
                breadth = unique_authors / post_count if post_count else 0.0
                source_days[day_key] = {
                    "status": "observed",
                    "sampleCount": max(0, safe_int(raw_row.get("sampleCount"))),
                    "signals": signals,
                    "volume": volume,
                    "uniqueAuthorRatio": breadth,
                }
                for signal_key, value in signals.items():
                    references[platform][day_key][signal_key].append(float(value))
                references[platform][day_key]["volume"].append(volume)
                references[platform][day_key]["breadth"].append(breadth)
            elif day_key in verified_zeros:
                source_days[day_key] = {
                    "status": "verified_zero",
                    "sampleCount": 0,
                    "volume": 0.0,
                }
                references[platform][day_key]["volume"].append(0.0)
                references[platform][day_key]["breadth"].append(0.0)
            else:
                source_days[day_key] = {"status": "unobserved", "sampleCount": 0}
        base_rows[source_key] = source_days

    calibrated: Dict[str, Dict[str, Dict[str, object]]] = {}
    for source_key, source_days in base_rows.items():
        platform = str(results[source_key].get("platform") or "unknown")
        calibrated[source_key] = {}
        for day_key, row in source_days.items():
            if row.get("status") != "observed":
                calibrated[source_key][day_key] = dict(row)
                continue
            reference = references[platform][day_key]
            signals = dict(row.get("signals") or {})
            for signal_key in ("optimism", "fomo", "panic", "newcomer", "intensity"):
                absolute = float(signals.get(signal_key, FAIR_SIGNAL_BASELINES[signal_key]))
                values = reference.get(signal_key, [])
                if len(values) >= 8:
                    relative = percentile_score(values, absolute, low=20.0, high=80.0)
                    absolute_weight = ABSOLUTE_SIGNAL_WEIGHT.get(signal_key, 0.75)
                    signals[signal_key] = round1(
                        absolute_weight * absolute + (1.0 - absolute_weight) * relative
                    )
            volume_values = reference.get("volume", [])
            breadth_values = reference.get("breadth", [])
            if len(volume_values) >= 8:
                volume_score = percentile_score(
                    volume_values,
                    float(row.get("volume") or 0.0),
                    low=15.0,
                    high=85.0,
                )
                breadth_score = (
                    percentile_score(
                        breadth_values,
                        float(row.get("uniqueAuthorRatio") or 0.0),
                        low=25.0,
                        high=75.0,
                    )
                    if len(breadth_values) >= 8 else 50.0
                )
                signals["attention"] = round1(
                    ACTIVITY_VELOCITY_WEIGHT * volume_score
                    + ACTIVITY_BREADTH_WEIGHT * breadth_score
                )
            else:
                signals["attention"] = 50.0
            calibrated[source_key][day_key] = {
                **row,
                "signals": signals,
                "calibrationReferenceCount": len(volume_values),
            }
    return calibrated


def _fair_source_rows(
    results: Mapping[str, Dict[str, object]],
    source_keys: Sequence[str],
    analyses: Mapping[str, Mapping[str, object] | None],
    daily_rows: Mapping[str, Mapping[str, Mapping[str, object]]],
    unit_for_source: Mapping[str, str],
) -> List[Dict[str, object]]:
    rows: List[Dict[str, object]] = []
    for source_key in source_keys:
        source = results.get(source_key)
        unit_key = str(unit_for_source.get(source_key) or "")
        if not isinstance(source, Mapping) or not unit_key:
            continue
        rows.append({
            "sourceKey": source_key,
            "unitKey": unit_key,
            "companyKey": unit_key,
            "channel": str(source.get("platform") or "unknown"),
            "platform": str(source.get("platform") or "unknown"),
            "analysis": analyses.get(source_key) or {},
            "dailySignals": daily_rows.get(source_key, {}),
        })
    return rows


def _summarize_fairness(
    fair: Dict[str, object],
    unit_names: Mapping[str, str],
    source_rows: Sequence[Mapping[str, object]],
    trading_days: Sequence[date],
    channel_specs: Mapping[str, Mapping[str, object]],
) -> Dict[str, object]:
    diagnostics = dict(fair.get("diagnostics") or {})
    platform_summary: List[Dict[str, object]] = []
    for raw in diagnostics.get("platformSummary", []):
        if not isinstance(raw, Mapping):
            continue
        key = str(raw.get("key") or raw.get("channel") or "")
        platform_summary.append({
            **raw,
            "key": key,
            "name": PLATFORM_NAMES.get(key, key),
            "volumeEligible": bool(raw.get("allowAttention", key != "ths")),
            "formalWeightMeaning": "目标席位固定25%；证据不足部分收缩到中性，不转给其他渠道",
        })
    diagnostics["platformSummary"] = platform_summary

    labelled_units: Dict[str, List[Dict[str, object]]] = {}
    for channel, raw_rows in (diagnostics.get("unitWeights") or {}).items():
        labelled_units[str(channel)] = [
            {**dict(row), "name": unit_names.get(str(row.get("key") or ""), str(row.get("key") or ""))}
            for row in raw_rows if isinstance(row, Mapping)
        ] if isinstance(raw_rows, Sequence) else []
    diagnostics["unitWeights"] = labelled_units

    # Compatibility summary for the existing representative-company cards.
    company_keys = [
        key for key in unit_names
        if any(
            str(spec.get("mode") or "") == "company"
            and key in [str(item) for item in spec.get("expectedUnits", [])]
            for spec in channel_specs.values()
        )
    ]
    company_weights: List[Dict[str, object]] = []
    for company_key in company_keys:
        target = 0.0
        influence = 0.0
        for channel, spec in channel_specs.items():
            expected = [str(item) for item in spec.get("expectedUnits", [])]
            if str(spec.get("mode") or "") != "company" or company_key not in expected:
                continue
            target += 0.25 / max(1, len(expected))
            for row in labelled_units.get(str(channel), []):
                if str(row.get("key") or "") == company_key:
                    influence += float(row.get("formalInfluence") or 0.0)
        company_weights.append({
            "key": company_key,
            "name": unit_names.get(company_key, company_key),
            "targetWeight": round(target, 6),
            "effectiveWeight": round(influence, 6),
            "neutralisedWeight": round(max(0.0, target - influence), 6),
        })
    diagnostics["companyWeights"] = company_weights
    diagnostics["expectedCompanyCount"] = len(company_keys)
    observed_company_keys = {
        str(row.get("unitKey") or "")
        for row in source_rows
        if str(row.get("unitKey") or "") in company_keys and bool((row.get("analysis") or {}).get("sampleCount"))
    }
    diagnostics["providedCompanyCount"] = len(observed_company_keys)

    daily_diagnostics = [
        row for row in diagnostics.get("effectiveDayWeights", []) if isinstance(row, Mapping)
    ]
    point_coverage = diagnostics.get("channelPointCoverage") if isinstance(diagnostics.get("channelPointCoverage"), Mapping) else {}
    coverage = sum(
        float((values or {}).get("optimism") or 0.0)
        for values in point_coverage.values() if isinstance(values, Mapping)
    ) / max(1, len(FAIR_CHANNELS))
    latest_channels = daily_diagnostics[0].get("channels", {}) if daily_diagnostics else {}
    latest_observed = sum(
        isinstance(row, Mapping) and str(row.get("status") or "") == "observed"
        for row in latest_channels.values()
    ) if isinstance(latest_channels, Mapping) else 0
    reliability = float(diagnostics.get("reliabilityPercent") or 0.0)

    base_composite = float((fair.get("scores") or {}).get("composite", 50.0))
    sensitivity_values = [base_composite]
    for omitted_channel in FAIR_CHANNELS:
        if any(str(row.get("channel") or "") == omitted_channel for row in source_rows):
            trial_rows = [
                row for row in source_rows
                if str(row.get("channel") or "") != omitted_channel
            ]
            trial = aggregate_channel_fairness(
                trial_rows,
                [day.isoformat() for day in trading_days],
                channel_specs,
            )
            sensitivity_values.append(float((trial.get("scores") or {}).get("composite", 50.0)))
    stability_low = min(sensitivity_values)
    stability_high = max(sensitivity_values)
    stability_delta = max(abs(value - base_composite) for value in sensitivity_values)
    concentration = diagnostics.get("concentration") if isinstance(diagnostics.get("concentration"), Mapping) else {}
    observed_channels = safe_int((fair.get("metrics") or {}).get("observedChannelCount"))
    rank_eligible = bool(
        fair.get("available")
        and observed_channels >= 3
        and coverage >= 0.45
        and latest_observed >= 2
        and reliability >= 20.0
        and stability_delta <= 10.0
        and not bool(concentration.get("capBreached"))
    )
    if reliability >= 55.0 and coverage >= 0.80 and observed_channels == 4 and stability_delta <= 5.0:
        confidence_label = "高"
    elif reliability >= 20.0 and coverage >= 0.45 and observed_channels >= 3 and stability_delta <= 10.0:
        confidence_label = "中"
    else:
        confidence_label = "低"
    diagnostics.update({
        "modelVersion": FAIRNESS_MODEL_VERSION,
        # Compatibility field retained; the precise meaning is that sample
        # proportions never buy a larger seat. Each unit's own n may only
        # shrink that fixed seat toward neutral.
        "formalPointEstimateNotSampleWeighted": True,
        "formalPointEstimateNotSampleProportionWeighted": True,
        "sampleCountAffectsOwnNeutralShrinkOnly": True,
        "fixedChannelTargetWeight": 0.25,
        "channelWeightsNeverRedistributed": True,
        "weightedCompanyCoverage": round(coverage, 6),
        "weightedChannelCoverage": round(coverage, 6),
        "latestObservedCompanyCount": len(observed_company_keys),
        "latestObservedChannelCount": latest_observed,
        "observedChannelCount": observed_channels,
        "rankEligible": rank_eligible,
        "confidenceLabel": confidence_label,
        "stabilityBand": {
            "type": "leave_one_channel_out",
            "low": round(stability_low, 1),
            "high": round(stability_high, 1),
            "maxDelta": round(stability_delta, 1),
            "isStatisticalConfidenceInterval": False,
        },
    })
    return diagnostics


def score_fair_group(
    results: Mapping[str, Dict[str, object]],
    source_keys: Sequence[str],
    analyses: Mapping[str, Mapping[str, object] | None],
    daily_rows: Mapping[str, Mapping[str, Mapping[str, object]]],
    unit_for_source: Mapping[str, str],
    unit_names: Mapping[str, str],
    trading_days: Sequence[date],
    channel_specs: Mapping[str, Mapping[str, object]],
) -> Dict[str, object]:
    source_rows = _fair_source_rows(
        results,
        source_keys,
        analyses,
        daily_rows,
        unit_for_source,
    )
    fair = aggregate_channel_fairness(
        source_rows,
        [day.isoformat() for day in trading_days],
        channel_specs,
    )
    fair["diagnostics"] = _summarize_fairness(
        fair,
        unit_names,
        source_rows,
        trading_days,
        channel_specs,
    )
    volume_rows = [
        row for row in source_rows
        if str(row.get("channel") or "") != "ths"
        and isinstance(row.get("analysis"), Mapping)
    ]
    lower_bound = sum(
        float((row.get("analysis", {}).get("activity") or {}).get("weightedDailyPosts3d") or 0.0)
        for row in volume_rows
    )
    expected_volume_units = sum(
        len(spec.get("expectedUnits", []))
        for channel, spec in channel_specs.items()
        if bool(spec.get("allowAttention", True))
    )
    observed_volume_units = len({
        (str(row.get("channel") or ""), str(row.get("unitKey") or ""))
        for row in volume_rows if bool((row.get("analysis") or {}).get("sampleCount"))
    })
    fair["metrics"] = {
        **dict(fair.get("metrics") or {}),
        "coverageAdjustedVolume": round(lower_bound, 3),
        "weightedVolumeLowerBound": round(lower_bound, 3),
        "volumeCoverage": round(observed_volume_units / max(1, expected_volume_units), 6),
    }
    return fair


def capped_distribution(raw_weights: Sequence[float], cap: float, target: float = 1.0) -> List[float]:
    if not raw_weights or target <= 0:
        return [0.0 for _ in raw_weights]
    target = min(target, cap * len(raw_weights))
    remaining = set(range(len(raw_weights)))
    result = [0.0] * len(raw_weights)
    remaining_target = target
    while remaining and remaining_target > 1e-9:
        total = sum(max(raw_weights[index], 1e-9) for index in remaining)
        newly_capped: List[int] = []
        for index in remaining:
            proposed = remaining_target * max(raw_weights[index], 1e-9) / total
            if proposed >= cap - 1e-9:
                result[index] = cap
                newly_capped.append(index)
        if not newly_capped:
            for index in remaining:
                result[index] = remaining_target * max(raw_weights[index], 1e-9) / total
            break
        for index in newly_capped:
            remaining.remove(index)
            remaining_target -= cap
    return result


def select_samples(
    analyses: Sequence[Dict[str, object]],
    limit: int = 10,
    as_of: datetime = NOW,
    record_type: str = MEASURED,
) -> List[Dict[str, object]]:
    pool: List[Dict[str, object]] = []
    for analysis in analyses:
        pool.extend(analysis.get("posts", []))
    pool.sort(
        key=lambda post: (
            abs(int(post.get("rawScore") or 0)) * 2.5
            + (math.log1p(safe_int(post.get("comments"))) if record_type == MEASURED else 0.0)
            + (2.0 if post_age_days(post, as_of) <= 1 else 0.0)
            - post_age_days(post, as_of) * 0.05
        ),
        reverse=True,
    )
    selected: List[Dict[str, object]] = []
    platform_counts: Dict[str, int] = defaultdict(int)
    for post in pool:
        platform = str(post.get("platform") or "unknown")
        if platform_counts[platform] >= 4:
            continue
        selected.append({
            "title": post.get("title"),
            "label": post.get("label"),
            "score": safe_int(post.get("rawScore")),
            "comments": safe_int(post.get("comments")) if record_type == MEASURED else 0,
            "clicks": safe_int(post.get("clicks")) if record_type == MEASURED else 0,
            "source": post.get("source") or PLATFORM_NAMES.get(platform, "公开社区"),
            "platform": platform,
            "platformName": PLATFORM_NAMES.get(platform, platform),
            "publishTime": post.get("publishTime"),
        })
        platform_counts[platform] += 1
        if len(selected) >= limit:
            break
    return selected


def analyze_group(
    posts: Sequence[Dict[str, object]],
    calibration: Mapping[str, Mapping[str, Sequence[float]]] | None = None,
    as_of: datetime = NOW,
    record_type: str = MEASURED,
) -> Dict[str, object]:
    by_source: Dict[str, List[Dict[str, object]]] = defaultdict(list)
    for post in posts:
        by_source[str(post.get("sourceKey") or "unknown")].append(post)
    source_rows: List[Tuple[str, Dict[str, object]]] = []
    for source_key, source_posts in by_source.items():
        analysis = source_analysis(source_posts, as_of, record_type)
        if analysis:
            platform = str(source_posts[0].get("platform") or source_key.split(":", 1)[0])
            if calibration:
                analysis = calibrate_source_analysis(analysis, platform, calibration)
            source_rows.append((source_key, analysis))

    if not source_rows:
        empty = {key: 50.0 for key in (*SIGNAL_KEYS, "heat")}
        empty["panic"] = 0.0
        return {
            "available": False,
            "signals": empty,
            "scores": score_personas(empty, 50.0),
            "diagnostics": {
                "totalPosts": len(posts), "recentPosts": 0, "recent7Posts": 0,
                "samplePosts": 0, "bullishPosts": 0, "bearishPosts": 0,
                "neutralPosts": 0, "bullishRatio": 0.0, "bearishRatio": 0.0,
                "fomoRate": 0.0, "panicRate": 0.0, "newcomerRate": 0.0,
                "platformCount": 0, "sourceCount": 0, "activityIndex": 50.0,
                "estimatedDailyPosts": 0.0, "uniqueAuthors24": 0,
                "weightedDailyPosts3d": 0.0, "tradingWindowPosts": 0, "uniqueAuthors3d": 0,
                "personaEvidence": persona_evidence_diagnostics([], 50.0, as_of),
            },
            "samplePosts": [], "platforms": [], "breadth": 50.0,
        }

    by_platform: Dict[str, List[Tuple[str, Dict[str, object]]]] = defaultdict(list)
    for source_key, analysis in source_rows:
        platform = source_key.split(":", 1)[0]
        by_platform[platform].append((source_key, analysis))

    platform_rows: List[Dict[str, object]] = []
    for platform, rows in by_platform.items():
        reliability = [min(1.0, math.sqrt(float(item["sampleCount"]) / 20.0)) for _, item in rows]
        source_cap = 1.0 if len(rows) == 1 else 0.60 if len(rows) == 2 else 0.55
        source_weights = capped_distribution(reliability, cap=source_cap, target=1.0)
        signals = {
            key: round1(sum(weight * float(item["signals"][key]) for weight, (_, item) in zip(source_weights, rows)))
            for key in (*SIGNAL_KEYS, "panic", "heat")
        }
        raw = {
            key: sum(weight * float(item["raw"][key]) for weight, (_, item) in zip(source_weights, rows))
            for key in ("net", "fomo", "panic", "newcomer", "intense")
        }
        activity_rows = [item.get("activity", {}) for _, item in rows]
        activity_relative = sum(
            weight * float(activity.get("relativeScore") or 50.0)
            for weight, activity in zip(source_weights, activity_rows)
        )
        platform_rows.append({
            "key": platform,
            "name": PLATFORM_NAMES.get(platform, platform),
            # 同花顺当前公开端点是 hot_feed，不是严格时间流；它可以参与
            # 文本情绪，但在找到可验证时间流前不参与发帖速度排名。
            "activityEligible": platform in {"eastmoney", "sina"},
            "signals": signals,
            "raw": raw,
            "sampleCount": sum(int(item["sampleCount"]) for _, item in rows),
            "sourceCount": len(rows),
            "recent24": sum(int(item["recent24"]) for _, item in rows),
            "activity": {
                "relativeScore": round1(activity_relative),
                "postingVelocity": round(sum(
                    float(activity.get("postingVelocity") or 0.0)
                    for activity in activity_rows
                ), 3),
                "weightedDailyPosts3d": round(sum(
                    float(activity.get("weightedDailyPosts3d") or activity.get("postingVelocity") or 0.0)
                    for activity in activity_rows
                ), 3),
                "uniqueAuthors3d": sum(
                    safe_int(activity.get("uniqueAuthors3d")) for activity in activity_rows
                ),
                "uniqueAuthors24": sum(
                    safe_int(activity.get("uniqueAuthors24")) for activity in activity_rows
                ),
                "lowerBoundSourceCount": sum(
                    bool(activity.get("lowerBound")) for activity in activity_rows
                ),
            },
            "reliability": min(1.0, math.sqrt(sum(int(item["sampleCount"]) for _, item in rows) / 24.0)),
        })

    # 缺平台属于覆盖度问题，不是“中性情绪”。只在真实可用的平台之间归一化，
    # 再通过 diagnostics / history.coverage 降低置信度，避免默认 50 污染指数。
    platform_cap = 1.0 if len(platform_rows) == 1 else 0.65 if len(platform_rows) == 2 else 0.40
    platform_weights = capped_distribution(
        [float(row["reliability"]) for row in platform_rows],
        cap=platform_cap,
        target=1.0,
    )
    signals: Dict[str, float] = {}
    for key in SIGNAL_KEYS:
        value = normalized_weighted_average(
            (weight, float(row["signals"][key])) for weight, row in zip(platform_weights, platform_rows)
        )
        signals[key] = round1(value if value is not None else 50.0)
    panic_value = normalized_weighted_average(
        (weight, float(row["signals"]["panic"])) for weight, row in zip(platform_weights, platform_rows)
    )
    signals["panic"] = round1(panic_value if panic_value is not None else 0.0)
    signals["heat"] = round1(
        signals["fomo"] * 0.30 + signals["newcomer"] * 0.15
        + signals["intensity"] * 0.25 + signals["attention"] * 0.30
    )
    activity_index_value = normalized_weighted_average(
        (
            weight if bool(row.get("activityEligible")) else 0.0,
            float(row.get("activity", {}).get("relativeScore") or 50.0),
        )
        for weight, row in zip(platform_weights, platform_rows)
    )
    activity_index = round1(activity_index_value if activity_index_value is not None else signals["attention"])

    confidence = min(1.0, len(source_rows) / 5.0)
    hot_share = sum(1 for _, analysis in source_rows if float(analysis["signals"]["heat"]) >= 50.0) / len(source_rows)
    breadth = round1(50.0 + (hot_share - 0.5) * 100.0 * confidence)
    scores = score_personas(signals, breadth)

    annotated = [analysis for _, analysis in source_rows]
    persona_diagnostics = persona_evidence_diagnostics(annotated, signals["optimism"], as_of)
    sample_count = sum(int(item["sampleCount"]) for item in annotated)
    bullish = sum(int(item["bullish"]) for item in annotated)
    bearish = sum(int(item["bearish"]) for item in annotated)
    neutral = sum(int(item["neutral"]) for item in annotated)
    raw_weights = [int(item["sampleCount"]) for item in annotated]
    raw_total = max(sum(raw_weights), 1)
    raw_aggregate = {
        key: sum(weight * float(item["raw"][key]) for weight, item in zip(raw_weights, annotated)) / raw_total
        for key in ("fomo", "panic", "newcomer")
    }
    for weight, row in zip(platform_weights, platform_rows):
        row["weight"] = round(weight * 100.0, 1)
        row.pop("reliability", None)
    attention_status = Counter(
        str(item.get("attentionBaseline", {}).get("status") or "unknown") for item in annotated
    )
    attention_reasons = Counter(
        str(item.get("attentionBaseline", {}).get("reason"))
        for item in annotated if item.get("attentionBaseline", {}).get("reason")
    )
    sampling_rows = [item.get("samplingPolicy", {}) for item in annotated]
    window_days = scoring_trading_days(as_of)
    daily_count_map: Dict[str, Dict[str, float]] = {
        day.isoformat(): {"postCount": 0.0, "equivalentPosts": 0.0, "uniqueAuthors": 0.0}
        for day in window_days
    }
    for item in annotated:
        activity = item.get("activity") if isinstance(item.get("activity"), Mapping) else {}
        rows = activity.get("dailyPostCounts") if isinstance(activity.get("dailyPostCounts"), list) else []
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            day_text = str(row.get("date") or "")
            if day_text not in daily_count_map:
                continue
            daily_count_map[day_text]["postCount"] += max(0, safe_int(row.get("postCount")))
            daily_count_map[day_text]["equivalentPosts"] += max(0.0, float(row.get("equivalentPosts") or 0.0))
            daily_count_map[day_text]["uniqueAuthors"] += max(0, safe_int(row.get("uniqueAuthors")))
    group_effective_weights = normalized_trading_day_weights(
        as_of,
        (
            date.fromisoformat(day_text)
            for day_text, counts in daily_count_map.items()
            if counts["postCount"] > 0
        ),
    )
    daily_post_counts = [
        {
            "date": day.isoformat(),
            "rawWeight": TRADING_DAY_WEIGHTS[index],
            "effectiveWeight": round(group_effective_weights.get(day, 0.0), 6),
            "postCount": int(daily_count_map[day.isoformat()]["postCount"]),
            "equivalentPosts": round(daily_count_map[day.isoformat()]["equivalentPosts"], 1),
            "uniqueAuthors": int(daily_count_map[day.isoformat()]["uniqueAuthors"]),
            "available": daily_count_map[day.isoformat()]["postCount"] > 0,
        }
        for index, day in enumerate(window_days)
    ]
    diagnostics = {
        "totalPosts": len(posts),
        "recentPosts": sum(int(item["recent24"]) for item in annotated),
        "recent7Posts": sum(int(item["recent7"]) for item in annotated),
        "samplePosts": sample_count,
        "bullishPosts": bullish,
        "bearishPosts": bearish,
        "neutralPosts": neutral,
        "bullishRatio": round(bullish / max(sample_count, 1) * 100.0, 1),
        "bearishRatio": round(bearish / max(sample_count, 1) * 100.0, 1),
        "fomoRate": round(raw_aggregate["fomo"] * 100.0, 1),
        "panicRate": round(raw_aggregate["panic"] * 100.0, 1),
        "newcomerRate": round(raw_aggregate["newcomer"] * 100.0, 1),
        "platformCount": len(platform_rows),
        "sourceCount": len(source_rows),
        "activityIndex": activity_index,
        "estimatedDailyPosts": round(sum(
            float(item.get("activity", {}).get("postingVelocity") or 0.0)
            for item in annotated
        ), 1),
        "weightedDailyPosts3d": round(sum(
            float(item.get("activity", {}).get("weightedDailyPosts3d") or 0.0)
            for item in annotated
        ), 1),
        "tradingDays": [day.isoformat() for day in window_days],
        "dailyPostCounts": daily_post_counts,
        "currentTradingDayPosts": int(daily_post_counts[0]["postCount"]) if daily_post_counts else 0,
        "activityVelocityWeight": ACTIVITY_VELOCITY_WEIGHT,
        "activityBreadthWeight": ACTIVITY_BREADTH_WEIGHT,
        "tradingWindowPosts": sum(
            safe_int(item.get("tradingWindowPosts")) for item in annotated
        ),
        "uniqueAuthors3d": sum(
            safe_int(item.get("activity", {}).get("uniqueAuthors3d")) for item in annotated
        ),
        "uniqueAuthors24": sum(
            safe_int(item.get("activity", {}).get("uniqueAuthors24")) for item in annotated
        ),
        "activityLowerBoundSourceCount": sum(
            bool(item.get("activity", {}).get("lowerBound")) for item in annotated
        ),
        "available": True,
        "personaEvidence": persona_diagnostics,
        "samplingPolicy": {
            "mode": record_type,
            "capPerSourcePublishedDay": ESTIMATED_SOURCE_DAY_LIMIT if record_type != MEASURED else None,
            "postsBefore": sum(safe_int(item.get("postsBefore")) for item in sampling_rows),
            "postsAfterAuthorLimit": sum(safe_int(item.get("postsAfterAuthorLimit")) for item in sampling_rows),
            "postsSelected": sum(safe_int(item.get("postsSelected")) for item in sampling_rows),
            "activityPosts": sum(safe_int(item.get("activityPostCount")) for item in annotated),
            "sourceDayBucketsCapped": sum(safe_int(item.get("sourceDayBucketsCapped")) for item in sampling_rows),
            "selectionUsesSentiment": False,
        },
        "attentionBaseline": {
            "mode": record_type,
            "method": "three_trading_day_weighted_post_volume",
            "sourceStatusCounts": dict(attention_status),
            "fallbackReasons": dict(attention_reasons),
            "missingDaysAssumedZero": False,
            "tradingDayWeights": list(TRADING_DAY_WEIGHTS),
            "usesFutureDates": False,
            "usesInteractions": False,
        },
    }
    return {
        "available": True,
        "signals": signals,
        "scores": scores,
        "diagnostics": diagnostics,
        "samplePosts": select_samples(annotated, as_of=as_of, record_type=record_type),
        "platforms": platform_rows,
        "breadth": breadth,
    }


def score_personas(signals: Mapping[str, float], breadth: float) -> Dict[str, int]:
    optimism = float(signals.get("optimism", 50.0))
    fomo = float(signals.get("fomo", 50.0))
    newcomer = float(signals.get("newcomer", 50.0))
    intensity = float(signals.get("intensity", 50.0))
    attention = float(signals.get("attention", 50.0))
    baoma = (
        optimism * 0.25 + fomo * 0.25 + newcomer * 0.15
        + intensity * 0.15 + attention * 0.15 + breadth * 0.05
    )
    novice = (
        optimism * 0.15 + fomo * 0.20 + newcomer * 0.30
        + intensity * 0.15 + attention * 0.15 + breadth * 0.05
    )
    retail = (
        optimism * 0.30 + fomo * 0.20 + newcomer * 0.05
        + intensity * 0.15 + attention * 0.20 + breadth * 0.10
    )
    scores = {
        "baoma": int(round(clamp(baoma))),
        "novice": int(round(clamp(novice))),
        "retail": int(round(clamp(retail))),
    }
    scores["composite"] = int(round(sum(scores.values()) / 3.0))
    return scores


def source_public_info(result: Mapping[str, object]) -> Dict[str, object]:
    return {
        "code": result.get("code"),
        "name": result.get("name"),
        "url": result.get("url"),
        "platform": result.get("platform"),
        "platformName": PLATFORM_NAMES.get(str(result.get("platform")), str(result.get("platform"))),
        "fresh": bool(result.get("fresh")),
        "coverage": {
            "rawRows": safe_int(result.get("rawRows")),
            "pageCount": safe_int(result.get("pageCount")),
            "oldestSeen": result.get("oldestSeen"),
            "newestSeen": result.get("newestSeen"),
            "capHit": result.get("capHit"),
            "chronological": bool(result.get("chronological")),
            "coverageDays": list(result.get("coverageDays") or []),
            "thirdTradingDayReached": bool(result.get("thirdTradingDayReached")),
            "freshFetchedNow": bool(result.get("freshFetchedNow")),
            "cacheAge": result.get("cacheAge"),
            "coverageStatus": result.get("coverageStatus"),
            "paginationMode": result.get("paginationMode"),
            "windowStartCovered": bool(result.get("windowStartCovered")),
            "chronologicalBoundary": result.get("chronologicalBoundary"),
            "archiveReusedPosts": safe_int(result.get("archiveReusedPosts")),
        },
    }


def sector_attention_weights(sectors: Sequence[Dict[str, object]]) -> List[float]:
    if not sectors:
        return []
    equal = 1.0 / len(sectors)
    reliability = [
        math.sqrt(
            min(1.0, float(item.get("metrics", {}).get("sourceCount", 0)) / max(1, int(item.get("expectedSourceCount") or 5)))
            * min(1.0, float(item.get("metrics", {}).get("platformCount", 0)) / len(PLATFORM_NAMES))
        )
        for item in sectors
    ]
    reliability_total = max(sum(reliability), 1e-9)
    attention = [
        max(1.0, float(item["signals"].get("attention", 50.0))) * max(0.15, quality)
        for item, quality in zip(sectors, reliability)
    ]
    total_attention = sum(attention)
    # “等权”部分也按真实覆盖质量分配；缺来源通过低权重/低置信度表达，
    # 不用中性 50 伪造一个完整板块。
    raw = [
        0.5 * quality / reliability_total + 0.5 * value / total_attention
        for quality, value in zip(reliability, attention)
    ]
    return capped_distribution(raw, cap=2.0 * equal, target=1.0)


def aggregate_sector_parents(
    sectors: Sequence[Dict[str, object]],
) -> List[Dict[str, object]]:
    """Use fixed leaf seats inside fixed parent seats.

    A hotter child, an extra platform, or a missing sibling must never acquire
    another child's vote.  Missing leaves retain a neutral seat and are exposed
    as lower coverage instead of silently donating their weight.
    """

    grouped: Dict[str, List[Dict[str, object]]] = defaultdict(list)
    expected_counts: Counter[str] = Counter()
    parent_names: Dict[str, str] = {}
    for item in sectors:
        sector_key = str(item.get("key") or "")
        parent_key = str(item.get("parentKey") or sector_key)
        parent_name = str(item.get("parentName") or item.get("name") or parent_key)
        expected_counts[parent_key] += 1
        parent_names[parent_key] = parent_name
        if bool(item.get("available")):
            grouped[parent_key].append(item)

    ordered_parent_keys = list(PARENT_SECTORS)
    ordered_parent_keys.extend(sorted(set(grouped) - set(ordered_parent_keys)))
    rows: List[Dict[str, object]] = []
    for parent_key in ordered_parent_keys:
        children = grouped.get(parent_key, [])
        expected = max(1, int(expected_counts.get(parent_key) or sum(
            1 for definition in SECTORS.values()
            if str(definition.get("parentKey") or "") == parent_key
        )))
        signals: Dict[str, float] = {}
        for signal_key in (*SIGNAL_KEYS, "panic", "heat"):
            baseline = FAIR_SIGNAL_BASELINES.get(signal_key, 50.0)
            deviation = sum(
                float(child["signals"].get(signal_key, baseline)) - baseline
                for child in children
            ) / expected
            signals[signal_key] = round1(baseline + deviation)
        rows.append({
            "key": parent_key,
            "name": parent_names.get(parent_key, PARENT_SECTORS.get(parent_key, parent_key)),
            "available": bool(children),
            "availableChildCount": len(children),
            "expectedChildCount": expected,
            "coverage": round(len(children) / expected, 6),
            "childWeight": round(1.0 / expected, 6),
            "missingChildrenNeutralised": max(0, expected - len(children)),
            "signals": signals,
        })
    return rows


def prepare_history() -> List[Dict[str, object]]:
    old_latest = load_json(LATEST_PATH, {})
    old_history = load_json(HISTORY_PATH, [])
    # 只有明确读到带版本号的旧快照才执行版本迁移。latest.json 暂时缺失、
    # 损坏或尚未写完时，绝不能把一份完好的历史静默清空。
    has_explicit_version = isinstance(old_latest, dict) and "version" in old_latest
    old_version = safe_int(old_latest.get("version"), VERSION) if has_explicit_version else VERSION
    if has_explicit_version and old_version < VERSION:
        if isinstance(old_history, list) and old_history:
            backup = V3_HISTORY_PATH if old_version == 3 else LEGACY_HISTORY_PATH
            if not backup.exists():
                save_json(backup, old_history)
        # Keep the old points visible, but never pass them off as comparable to
        # the fixed-channel v6 series.  The methodology segment is attached
        # below and the first v6 update starts a new measured regime.
    history = old_history if isinstance(old_history, list) else []
    upgraded: List[Dict[str, object]] = []
    for raw in history:
        if not isinstance(raw, Mapping):
            continue
        point = dict(raw)
        point.setdefault("methodology", "legacy_unspecified")
        try:
            point_day = date.fromisoformat(str(point.get("date") or ""))
        except ValueError:
            continue
        # 新口径的历史横轴只保留真实交易日；旧周末/休市点在下次更新时清理。
        if not is_trading_day(point_day):
            continue
        sectors = [
            dict(item) for item in point.get("sectors", [])
            if isinstance(item, Mapping)
        ]
        if sectors:
            # 只迁移旧结构；已经过 v2 截面校准或因果平滑的历史点必须原样
            # 保留，否则每天点击更新都会把历史平滑结果重新覆盖成原始值。
            needs_heat_upgrade = any(
                item.get("relativeHeat") is None
                or not isinstance(item.get("heatModel"), Mapping)
                for item in sectors
            )
            if needs_heat_upgrade:
                calibrate_sector_heats(sectors, float(point.get("heat") or 50.0))
            point["sectors"] = sectors
        upgraded.append(point)
    return upgraded


def score_results_as_of(
    results: Mapping[str, Dict[str, object]],
    market_keys: Sequence[str],
    sector_source_keys: Mapping[str, Sequence[str]],
    *,
    as_of: datetime = NOW,
    record_type: str = MEASURED,
) -> Dict[str, object]:
    """从一份原始文本集合计算指定时点快照，不执行任何联网请求。

    所有底层分析都收到同一个 ``as_of``，未来文本会在 source_analysis 中
    被排除。缺失的来源、平台和板块只降低覆盖度，不以 50 分混入总值。
    ``record_type != measured`` 时互动累计量不参与权重或活跃度。
    """

    trading_days = scoring_trading_days(as_of)
    analyses: Dict[str, Mapping[str, object] | None] = {
        key: source_analysis(source.get("posts", []), as_of, record_type)
        for key, source in results.items()
    }
    platform_calibration = build_platform_calibration(results, as_of, record_type)
    daily_calibrated_rows = build_daily_calibrated_source_rows(
        results,
        analyses,
        trading_days,
        as_of,
    )
    visible_source_keys = {
        key
        for key, analysis in analyses.items()
        if analysis is not None
    }
    visible_platforms = {
        str(results[key].get("platform") or "unknown")
        for key in visible_source_keys
    }

    sector_indices: List[Dict[str, object]] = []
    for sector_key, definition in SECTORS.items():
        keys = list(sector_source_keys.get(sector_key, []))
        posts = merge_posts(results, keys)
        analyzed = analyze_group(posts, platform_calibration, as_of, record_type)
        representatives = [
            dict(company)
            for company in definition.get("representativeCompanies", [])
            if isinstance(company, Mapping)
        ][:3]
        company_names = {
            str(company.get("code") or ""): str(company.get("name") or company.get("code") or "")
            for company in representatives
        }
        ths_communities = [
            dict(item) for item in definition.get("thsCommunities", [])
            if isinstance(item, Mapping)
        ]
        ths_codes = [str(item.get("code") or "") for item in ths_communities if item.get("code")]
        unit_names = {
            **company_names,
            **{
                str(item.get("code") or ""): str(item.get("name") or item.get("code") or "")
                for item in ths_communities
            },
        }
        channel_specs = {
            "eastmoney": {"mode": "company", "expectedUnits": list(company_names), "allowAttention": True},
            "sina": {"mode": "company", "expectedUnits": list(company_names), "allowAttention": True},
            "ths": {"mode": "sector", "expectedUnits": ths_codes, "allowAttention": False},
            "taoguba": {"mode": "company", "expectedUnits": list(company_names), "allowAttention": True},
        }
        unit_for_source: Dict[str, str] = {}
        for source_key in keys:
            if source_key not in results:
                continue
            platform = str(results[source_key].get("platform") or "")
            source_code = str(results[source_key].get("code") or "")
            if not source_code and platform in {"eastmoney", "sina", "taoguba"}:
                source_code = source_key.split(":", 1)[-1]
            expected_units = [str(item) for item in channel_specs.get(platform, {}).get("expectedUnits", [])]
            if source_code in expected_units:
                unit_for_source[source_key] = source_code
        fair = score_fair_group(
            results,
            keys,
            analyses,
            daily_calibrated_rows,
            unit_for_source,
            unit_names,
            trading_days,
            channel_specs,
        )
        diagnostics = analyzed["diagnostics"]
        fairness = fair["diagnostics"]
        formal_signals = fair["signals"]
        formal_scores = fair["scores"]
        working_sources = [
            source_public_info(results[key])
            for key in keys if key in visible_source_keys
        ]
        platform_summary = {
            str(row.get("key")): row
            for row in fairness.get("platformSummary", [])
            if isinstance(row, Mapping)
        }
        public_platforms = []
        for row in analyzed["platforms"]:
            summary = platform_summary.get(str(row.get("key")), {})
            public_platforms.append({
                **row,
                "weight": round(float(summary.get("emotionWeight") or 0.0) * 100.0, 1),
                "attentionWeight": round(float(summary.get("attentionWeight") or 0.0) * 100.0, 1),
                "targetWeight": round(float(summary.get("targetWeight") or 0.25) * 100.0, 1),
                "formalInfluence": round(float(summary.get("formalInfluence") or 0.0) * 100.0, 1),
                "formalWeightMeaning": "正式情绪/关注影响；缺失部分保留中性席位",
                "diagnosticOnlyLegacySignals": True,
            })
        sector_indices.append({
            "key": sector_key,
            "name": definition["name"],
            "parentKey": definition.get("parentKey") or sector_key,
            "parentName": definition.get("parentName") or definition["name"],
            "description": str(definition.get("description") or ""),
            "representativeCompanies": representatives,
            "catalogVersion": SECTOR_CATALOG_VERSION,
            "available": bool(fair.get("available")),
            "expectedSourceCount": len(keys),
            "scores": formal_scores,
            "signals": formal_signals,
            "heat": formal_signals["heat"],
            "direction": formal_signals["optimism"],
            "metrics": {
                "recentPosts": diagnostics["recentPosts"],
                "recent7Posts": diagnostics["recent7Posts"],
                "samplePosts": diagnostics["samplePosts"],
                "bullishRatio": diagnostics["bullishRatio"],
                "bearishRatio": diagnostics["bearishRatio"],
                "fomoRate": diagnostics["fomoRate"],
                "panicRate": diagnostics["panicRate"],
                "newcomerRate": diagnostics["newcomerRate"],
                "activityIndex": formal_signals.get("attention", 50.0),
                "estimatedDailyPosts": fair.get("metrics", {}).get("coverageAdjustedVolume") or 0.0,
                "weightedDailyPosts3d": fair.get("metrics", {}).get("coverageAdjustedVolume") or 0.0,
                "weightedVolumeLowerBound": fair.get("metrics", {}).get("weightedVolumeLowerBound") or 0.0,
                "volumeCoverage": fair.get("metrics", {}).get("volumeCoverage") or 0.0,
                "effectiveSample": fair.get("metrics", {}).get("effectiveSample") or 0.0,
                "tradingDays": [day.isoformat() for day in trading_days],
                "dailyPostCounts": diagnostics.get("dailyPostCounts", []),
                "currentTradingDayPosts": diagnostics.get("currentTradingDayPosts", 0),
                "tradingWindowPosts": diagnostics.get("tradingWindowPosts", 0),
                "uniqueAuthors24": diagnostics.get("uniqueAuthors24", 0),
                "uniqueAuthors3d": diagnostics.get("uniqueAuthors3d", 0),
                "activityLowerBoundSourceCount": diagnostics.get("activityLowerBoundSourceCount", 0),
                "sourceCount": diagnostics["sourceCount"],
                "platformCount": diagnostics["platformCount"],
                "personaEvidence": diagnostics.get("personaEvidence", {}),
                "samplingPolicy": diagnostics.get("samplingPolicy", {}),
                "attentionBaseline": diagnostics.get("attentionBaseline", {}),
                "fairness": fairness,
            },
            "platforms": public_platforms,
            "forumSources": working_sources,
            "samplePosts": analyzed["samplePosts"],
        })

    available_sectors = [item for item in sector_indices if item["available"]]
    parent_rows = aggregate_sector_parents(sector_indices)
    sector_signals: Dict[str, float] | None = None
    sector_scores: Dict[str, int] | None = None
    breadth_score: float | None = None
    if parent_rows:
        # 大类严格固定席位：缺失大类保持中性，不把票转送给仍可用的大类。
        parent_weight = 1.0 / len(parent_rows)
        sector_signals = {
            key: round1(sum(
                parent_weight * float(item["signals"][key])
                for item in parent_rows
            ))
            for key in (*SIGNAL_KEYS, "panic", "heat")
        }
        hot_parent_count = sum(
            1 for item in parent_rows if float(item["signals"]["heat"]) > 50.0
        )
        cold_parent_count = sum(
            1 for item in parent_rows if float(item["signals"]["heat"]) < 50.0
        )
        breadth_score = round1(
            50.0 + 50.0 * (hot_parent_count - cold_parent_count) / len(parent_rows)
        )
        sector_scores = score_personas(sector_signals, breadth_score)

    market_posts = merge_posts(results, market_keys)
    broad_legacy = analyze_group(market_posts, platform_calibration, as_of, record_type)
    formal_market_keys = list(market_keys)
    eastmoney_market_units = [code for code, _ in MARKET_EASTMONEY_SOURCES]
    market_channel_specs = {
        "eastmoney": {"mode": "sector", "expectedUnits": eastmoney_market_units, "allowAttention": True},
        "sina": {"mode": "sector", "expectedUnits": [symbol for symbol, _ in MARKET_SINA_SOURCES], "allowAttention": True},
        "ths": {"mode": "sector", "expectedUnits": ["1A0001"], "allowAttention": False},
        "taoguba": {"mode": "sector", "expectedUnits": ["broad-market"], "allowAttention": True},
    }
    market_unit_for_source = {
        key: str(
            results[key].get("code")
            or (key.split(":", 1)[-1] if key.startswith("eastmoney:") else key.rsplit(":", 1)[-1])
        )
        for key in formal_market_keys if key in results
    }
    market_unit_names = {
        **{code: name for code, name in MARKET_EASTMONEY_SOURCES},
        **dict(MARKET_SINA_SOURCES),
        "1A0001": "同花顺上证指数讨论",
        "broad-market": "全市场社区席位",
    }
    broad_fair = score_fair_group(
        results,
        formal_market_keys,
        analyses,
        daily_calibrated_rows,
        market_unit_for_source,
        market_unit_names,
        trading_days,
        market_channel_specs,
    )
    broad_diagnostics = dict(broad_legacy.get("diagnostics") or {})
    broad_diagnostics.update({
        "activityIndex": broad_fair["signals"].get("attention", 50.0),
        "weightedDailyPosts3d": broad_fair.get("metrics", {}).get("coverageAdjustedVolume") or 0.0,
        "fairness": broad_fair["diagnostics"],
        "marketWindowCoverage": market_window_diagnostics(results, formal_market_keys, daily_calibrated_rows, as_of),
        "thsMarketFeedFormalEmotionTargetWeight": 0.25,
        "thsMarketFeedFormalAttentionWeight": 0.0,
    })
    broad = {
        **broad_legacy,
        "available": bool(broad_fair.get("available")),
        "signals": broad_fair["signals"],
        "scores": broad_fair["scores"],
        "breadth": broad_fair["breadth"],
        "diagnostics": broad_diagnostics,
    }
    broad_available = bool(broad.get("available"))
    if not broad_available and not available_sectors:
        raise RuntimeError(f"{as_of.strftime('%Y-%m-%d')} 没有可用于评分的公开社区文本")

    market_scores: Dict[str, int] = {}
    for key in ("baoma", "novice", "retail"):
        value = (
            0.40 * float(broad["scores"][key] if broad_available else 50.0)
            + 0.50 * float(sector_scores[key] if sector_scores else 50.0)
            + 0.10 * float(breadth_score if breadth_score is not None else 50.0)
        )
        market_scores[key] = int(round(clamp(value)))
    market_scores["composite"] = int(round(sum(market_scores.values()) / 3.0))

    market_signals: Dict[str, float] = {}
    for key in (*SIGNAL_KEYS, "panic"):
        baseline = FAIR_SIGNAL_BASELINES.get(key, 50.0)
        value = (
            0.40 * float(broad["signals"].get(key, baseline) if broad_available else baseline)
            + 0.60 * float(sector_signals.get(key, baseline) if sector_signals else baseline)
        )
        market_signals[key] = round1(value)
    market_signals["breadth"] = round1(
        breadth_score if breadth_score is not None else broad.get("breadth", 50.0)
    )
    heat_value = (
        0.40 * float(broad["signals"].get("heat", 50.0) if broad_available else 50.0)
        + 0.50 * float(sector_signals.get("heat", 50.0) if sector_signals else 50.0)
        + 0.10 * float(breadth_score if breadth_score is not None else 50.0)
    )
    market_signals["heat"] = round1(heat_value)

    # 正式三类指数与市场总热度保持原口径；板块页面热度另做一次最终截面
    # 校准，把“全市场整体冷热”和“同日板块相对声量”分开后再合成。
    sector_heat_audit = calibrate_sector_heats(sector_indices, market_signals["heat"])

    sector_indices.sort(
        key=lambda item: (
            bool(item.get("metrics", {}).get("fairness", {}).get("rankEligible")),
            bool(item["available"]),
            item["scores"]["composite"],
        ),
        reverse=True,
    )
    return {
        "scores": market_scores,
        "signals": market_signals,
        "sectorIndices": sector_indices,
        "broad": broad,
        "availableSourceKeys": sorted(visible_source_keys),
        "availableSourceCount": len(visible_source_keys),
        "availablePlatformCount": len(visible_platforms),
        "availableSectorCount": len(available_sectors),
        "availableParentSectorCount": sum(bool(item.get("available")) for item in parent_rows),
        "parentSectorSummary": parent_rows,
        "availableFreshSourceCount": sum(
            1 for key in visible_source_keys if bool(results[key].get("fresh"))
        ),
        "sectorHeatAudit": sector_heat_audit,
    }


def unique_sample_posts(posts: Sequence[Dict[str, object]], limit: int = 12) -> List[Dict[str, object]]:
    """Deduplicate display excerpts across sources before applying the limit."""
    seen: set[str] = set()
    selected: List[Dict[str, object]] = []
    for post in sorted(posts, key=lambda item: str(item.get("publishTime") or ""), reverse=True):
        key = normalize_text(str(post.get("title") or ""))
        if not key or key in seen:
            continue
        seen.add(key)
        selected.append(post)
        if len(selected) >= limit:
            break
    return selected


def build_snapshot() -> Dict[str, object]:
    market_day = scoring_market_day(NOW)
    specs, market_keys, sector_source_keys = build_source_specs()
    cached = load_forum_cache()
    # 中断后 45 分钟内重试时直接续用刚刚成功的来源，只补失败项。
    fresh_results: Dict[str, Dict[str, object]] = {
        key: {**value, "fresh": True, "resumedFromCache": True}
        for key, value in cached.items()
        if key in specs and cache_is_recent(value)
        and (not specs[key].get("marketWindow")
             or value.get("marketWindowPolicyVersion") == MARKET_WINDOW_POLICY_VERSION)
    }
    failures: List[str] = []
    total_sources = len(specs)
    completed = len(fresh_results)
    print(f"需要读取 {total_sources} 个公开社区来源（4个平台）……", flush=True)
    if completed:
        print(f"已续用本轮刚完成的 {completed} 个来源，只补读取其余来源。", flush=True)

    batches = (
        ("eastmoney", 10, [spec for spec in specs.values() if spec["platform"] == "eastmoney" and spec["key"] not in fresh_results]),
        ("sina", 5, [spec for spec in specs.values() if spec["platform"] == "sina" and spec["key"] not in fresh_results]),
        ("ths", 3, [spec for spec in specs.values() if spec["platform"] == "ths" and spec["key"] not in fresh_results]),
        ("taoguba", 6, [spec for spec in specs.values() if spec["platform"] == "taoguba" and spec["key"] not in fresh_results]),
    )

    def collect_platform_batch(
        platform: str,
        worker_count: int,
        batch: Sequence[Mapping[str, object]],
    ) -> Tuple[Dict[str, Dict[str, object]], List[str]]:
        local_results: Dict[str, Dict[str, object]] = {}
        local_failures: List[str] = []
        local_completed = 0
        if not batch:
            return local_results, local_failures
        with ThreadPoolExecutor(max_workers=worker_count) as pool:
            jobs = {pool.submit(fetch_source, spec): spec for spec in batch}
            for future in as_completed(jobs):
                spec = jobs[future]
                local_completed += 1
                try:
                    result = future.result()
                    if platform == "taoguba":
                        result = accumulate_taoguba_snapshots(
                            result,
                            cached.get(str(spec["key"])) if isinstance(cached, Mapping) else None,
                        )
                    local_results[str(spec["key"])] = result
                except Exception as exc:
                    local_failures.append(f"{spec['name']}[{spec['platform']}]: {exc}")
                print(
                    f"{PLATFORM_NAMES.get(platform, platform)} {local_completed}/{len(batch)}",
                    flush=True,
                )
                if platform == "ths":
                    save_json(THS_CONFIG_CACHE_PATH, THS_CONFIG)
        return local_results, local_failures

    # 四个站点互不依赖：各站仍遵守自己的低并发上限，但不再串行等待。
    # 这样不会增加任何单站压力，日常总耗时约等于最慢平台而非四者相加。
    with ThreadPoolExecutor(max_workers=len(batches)) as platform_pool:
        platform_jobs = {
            platform_pool.submit(collect_platform_batch, platform, workers, batch): (platform, batch)
            for platform, workers, batch in batches
        }
        for future in as_completed(platform_jobs):
            platform, batch = platform_jobs[future]
            local_results, local_failures = future.result()
            fresh_results.update(local_results)
            failures.extend(local_failures)
            completed += len(batch)
            # 每个平台结束即落盘；某站临时变慢或进程中断时，其余完成项可续用。
            partial_cache = {
                **{key: value for key, value in cached.items() if cache_is_usable(value)},
                **fresh_results,
            }
            save_forum_cache(partial_cache)
            if platform == "sina":
                save_json(SINA_BID_CACHE_PATH, SINA_BIDS)
            if platform == "ths":
                save_json(THS_CONFIG_CACHE_PATH, THS_CONFIG)
            print(
                f"{PLATFORM_NAMES.get(platform, platform)}批次完成；总进度 {completed}/{total_sources}",
                flush=True,
            )

    fresh_platforms = {str(result.get("platform")) for result in fresh_results.values() if result.get("posts")}
    minimum_fresh = max(20, int(total_sources * 0.30))
    if len(fresh_results) < minimum_fresh or len(fresh_platforms) < 3:
        raise RuntimeError(
            f"新鲜社区源仅成功 {len(fresh_results)}/{total_sources}、{len(fresh_platforms)} 个平台；已保留上一次成功结果"
        )

    results = dict(fresh_results)
    cached_used = 0
    for key in specs:
        if key not in results and key in cached and cache_is_usable(cached[key]):
            results[key] = {**cached[key], "fresh": False, "staleReason": "本次读取失败，使用72小时内缓存"}
            cached_used += 1
    save_forum_cache(results)
    save_json(SINA_BID_CACHE_PATH, SINA_BIDS)
    save_json(THS_CONFIG_CACHE_PATH, THS_CONFIG)
    archive_stats = archive_results(results, COMMUNITY_ARCHIVE_DIR, collected_at=NOW)
    days = scoring_trading_days(NOW)
    archived = load_archived_results(
        COMMUNITY_ARCHIVE_DIR,
        start=datetime.combine(days[-1], datetime.min.time(), tzinfo=CN_TZ),
        end=NOW, observed_at=NOW,
    )
    results = merge_window_evidence(results, archived, specs, NOW)
    scored = score_results_as_of(
        results,
        market_keys,
        sector_source_keys,
        as_of=NOW,
        record_type=MEASURED,
    )
    market_scores = scored["scores"]
    market_signals = scored["signals"]
    sector_indices = scored["sectorIndices"]
    broad = scored["broad"]
    history = prepare_history()
    date_key = market_day.isoformat()
    prior_points = [
        point for point in history
        if isinstance(point, Mapping)
        and str(point.get("date") or "") < date_key
        and isinstance(point.get("sectors"), list)
    ]
    if prior_points:
        prior_point = max(prior_points, key=lambda point: str(point.get("date") or ""))
        try:
            temporal_gap_days = trading_day_gap(
                datetime.strptime(str(prior_point.get("date")), "%Y-%m-%d").date(),
                market_day,
            )
        except ValueError:
            temporal_gap_days = 1
        temporal_audit = stabilize_sector_heats(
            sector_indices,
            prior_point.get("sectors", []),
            market_signals["heat"],
            gap_days=temporal_gap_days,
        )
        scored["sectorHeatAudit"] = {
            **dict(scored.get("sectorHeatAudit") or {}),
            **temporal_audit,
        }
    all_sample_posts = broad["diagnostics"]["samplePosts"] + sum(
        int(item["metrics"]["samplePosts"]) for item in sector_indices
    )
    history_point = build_history_point(
        as_of=market_day,
        scores=market_scores,
        signals=market_signals,
        sectors=sector_indices,
        platform_count=int(scored["availablePlatformCount"]),
        expected_platform_count=len(PLATFORM_NAMES),
        source_count=int(scored["availableSourceCount"]),
        expected_source_count=total_sources,
        sector_count=int(scored["availableSectorCount"]),
        expected_sector_count=len(SECTORS),
        sample_posts=all_sample_posts,
        record_type=MEASURED,
        fresh_source_count=int(scored["availableFreshSourceCount"]),
        captured_at=NOW,
        methodology="community_text_four_channel_grassroots_three_trading_day_v5_archived_window",
    )
    history = upsert_history(history, history_point)
    history_meta = build_history_meta(history, updated_at=NOW)
    previous_meta = load_json(HISTORY_META_PATH, {})
    if (
        isinstance(previous_meta, dict)
        and isinstance(previous_meta.get("retrospectiveArchive"), dict)
        and any(record_type_of(item) == ESTIMATED for item in history if isinstance(item, Mapping))
    ):
        history_meta["retrospectiveArchive"] = previous_meta["retrospectiveArchive"]

    all_sector_samples: List[Dict[str, object]] = []
    for item in sector_indices:
        all_sector_samples.extend(item["samplePosts"][:2])
    community_samples = unique_sample_posts(broad["samplePosts"] + all_sector_samples)
    platform_counts = {
        platform: sum(1 for result in results.values() if result.get("platform") == platform)
        for platform in PLATFORM_NAMES
    }
    confidence = int(history_point["confidence"])
    diagnostics = {
        **broad["diagnostics"],
        "marketDate": date_key,
        "scoringTradingDays": [day.isoformat() for day in scoring_trading_days(NOW)],
        "tradingDayWeights": list(TRADING_DAY_WEIGHTS),
        "calendarMode": calendar_mode(market_day),
        "sectorSamplePosts": sum(int(item["metrics"]["samplePosts"]) for item in sector_indices),
        "allSamplePosts": all_sample_posts,
        "platformCount": int(scored["availablePlatformCount"]),
        "sourceCount": int(scored["availableSourceCount"]),
        "freshSourceCount": int(scored["availableFreshSourceCount"]),
        "cachedSourceCount": cached_used,
        "collectedSourceCount": len(results),
        "configuredSourceCount": total_sources,
        "availableSectorCount": int(scored["availableSectorCount"]),
        "availableParentSectorCount": int(scored["availableParentSectorCount"]),
        "configuredSectorCount": len(SECTORS),
        "configuredParentSectorCount": len(PARENT_SECTORS),
        "sectorCatalogVersion": SECTOR_CATALOG_VERSION,
        "sectorCatalogId": SECTOR_CATALOG_ID,
        "sectorSourcePlan": {
            "eastmoneyStocksPerSector": 3,
            "sinaStocksPerSector": 3,
            "taogubaStocksPerSector": 3,
            "thsCommunitiesPerSector": "1-2",
            "thsRelatedScenesDiscovered": True,
            "broadIndustryForumsUsed": False,
            "representativeCompaniesPerSector": 3,
        },
        "parentSectorSummary": scored.get("parentSectorSummary", []),
        "coverage": history_point["coverage"],
        "archiveAddedPosts": archive_stats["added"],
        "archiveReusedPosts": sum(safe_int(row.get("archiveReusedPosts")) for row in results.values()),
        "archiveQualityUnknownSkipped": sum(safe_int(row.get("archiveQualityUnknownSkipped")) for row in results.values()),
        "archiveExistingPosts": archive_stats["existing"],
        "sectorHeatModel": scored.get("sectorHeatAudit", {}),
        "fairnessModelVersion": FAIRNESS_MODEL_VERSION,
        "rankEligibleSectorCount": sum(
            bool(item.get("metrics", {}).get("fairness", {}).get("rankEligible"))
            for item in sector_indices
        ),
        "lowConfidenceSectorCount": sum(
            item.get("metrics", {}).get("fairness", {}).get("confidenceLabel") == "低"
            for item in sector_indices
        ),
        "companyEqualWeighting": True,
        "channelEqualWeighting": True,
        "fixedChannelTargetWeight": 0.25,
        "grassrootsStyleWeighting": True,
        "missingEvidenceNeutralised": True,
        "thsExcludedFromFormalVolume": True,
        "chronologicalCoverageSourceCount": sum(
            bool(result.get("chronological")) and bool(result.get("thirdTradingDayReached"))
            for result in results.values()
        ),
        "hotSectorCount": sum(
            1 for item in sector_indices
            if item["available"] and float(item["signals"]["heat"]) >= 50.0
        ),
    }
    return {
        "version": VERSION,
        "sectorCatalogVersion": SECTOR_CATALOG_VERSION,
        "sectorCatalogId": SECTOR_CATALOG_ID,
        "methodology": "community_text_four_channel_grassroots_three_trading_day_v5_archived_window",
        "scoringModel": "four_channels_equal_then_units_equal_grassroots_50_30_20_v5_archived_window",
        "marketDate": date_key,
        "updatedAt": NOW.isoformat(),
        "confidence": confidence,
        "scores": market_scores,
        "signals": market_signals,
        "heat": market_signals["heat"],
        "direction": market_signals["optimism"],
        "diagnostics": diagnostics,
        "community": {
            "sources": [source_public_info(results[key]) for key in market_keys if key in results],
            "samplePosts": community_samples[:12],
            "platforms": [
                {"key": key, "name": PLATFORM_NAMES[key], "sourceCount": platform_counts[key]}
                for key in PLATFORM_NAMES
            ],
        },
        "sectorIndices": sector_indices,
        "history": history,
        "historyMeta": history_meta,
        "sources": {
            "community": "东方财富股吧 · 新浪股吧 · 同花顺板块社区/关联社区 · 淘股吧公开个股讨论",
            "window": "仅最近3个A股交易日文本；先在每个渠道内部按固定单位席位聚合，再以最近50%/上一交易日30%/上两个交易日20%合成；缺失渠道或单位保持中性且不重新分权",
            "tradingCalendar": f"A股交易日历（{calendar_mode(market_day)}）；周末与休市帖子不计分，非交易日更新归属最近交易日",
            "marketFormula": "固定席位为大盘社区40% + 板块社区50% + 热度扩散面10%；细分板块在大类内等权、8个大类再等权；缺失席位保持中性并降低置信度，不把权重转送给其余来源",
            "marketCommunityCoverage": "大盘入口：东方财富与新浪各读取上证、深证、创业板指数讨论，同花顺读取上证指数讨论；淘股吧指数页未通过近期样本验证，大盘席位保持未观测，不用个股或推荐榜替代。",
            "marketPriceUsed": False,
            "rqUsed": False,
            "xueqiuUsed": False,
            "xueqiuNote": "雪球匿名自动访问受登录与官方抓取规则限制，仅保留人工核验，不自动计分",
            "platformCalibration": "每个平台、每个交易日单独做横截面校准；情绪保留70%—75%绝对文本含义，关注度只比较同平台当日分位，避免网页容量制造热度",
            "channelFairness": "东方财富、新浪、同花顺、淘股吧固定各占25%；样本数只通过n/(n+12)决定该席位向中性收缩多少，不制造额外投票权；缺失席位不转让",
            "companyFairness": "东方财富、新浪、淘股吧各自在三家典型公司内固定等权；同花顺按明确配置的板块社区固定等权，概念页和关联场景先合成同花顺这一票",
            "grassrootsVoice": "只判断表达形态，不推断用户智力或真实身份：非认证普通评论、第一人称持仓感受和口语提问保持高权重；机构/官方账号剔除，长篇投研、多股票推广和专业模板对称降权",
            "sectorHeat": "板块主热度由固定渠道的关注度与情绪温度形成，再做同日稳健横截面定位；达到渠道覆盖、有效样本和留一渠道稳定性门槛才给正式名次",
            "sectorActivity": "关注度中发帖声量占85%、独立参与广度占15%；东方财富、新浪与淘股吧的可见时序样本参与，其中淘股吧标为20条匿名页下限；同花顺热门排序流只参与文本情绪，不参与发帖量和关注度",
            "sectorCatalog": f"细分板块目录 {SECTOR_CATALOG_ID}（版本{SECTOR_CATALOG_VERSION}）：30个细分板块、8个大类，每板块展示3家唯一主归类的典型公司",
            "sectorSourcePlan": f"共配置{total_sources}个公开社区入口；每个细分板块读取三家公司的东方财富、新浪、淘股吧社区，并读取1—2个同花顺板块社区及其关联场景；所有入口先在所属渠道内合并，不会把入口数变成额外渠道票",
            "representativeCompanyNote": "典型公司仅用于解释板块边界与定位论坛，不是指数成份股或投资推荐",
        },
        "failures": failures,
    }


def main() -> int:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    write_log("开始四平台固定渠道社区文本版更新")
    try:
        snapshot = build_snapshot()
        # data.js 是页面真正读取的提交点，最后替换；即使中途意外退出，页面
        # 也只会看到上一份完整快照，而不会看到半套新文件。
        save_json(HISTORY_PATH, snapshot["history"])
        save_json(HISTORY_META_PATH, snapshot["historyMeta"])
        save_json(LATEST_PATH, snapshot)
        save_text(DATA_JS_PATH, "window.INDEX_DATA = " + json.dumps(snapshot, ensure_ascii=False, indent=2) + ";\n")
        write_log(
            f"更新完成：综合{snapshot['scores']['composite']}，热度{snapshot['heat']}，"
            f"新鲜社区源{snapshot['diagnostics']['freshSourceCount']}/{snapshot['diagnostics']['sourceCount']}，"
            f"失败{len(snapshot['failures'])}个"
        )
        print("\n更新完成。", flush=True)
        print(
            f"综合 {snapshot['scores']['composite']} · 宝妈 {snapshot['scores']['baoma']} · "
            f"小白 {snapshot['scores']['novice']} · 散户 {snapshot['scores']['retail']}",
            flush=True,
        )
        print(
            f"社区热度 {snapshot['heat']} · 情绪方向 {snapshot['direction']} · "
            f"四平台共分析 {snapshot['diagnostics']['allSamplePosts']} 条文本样本。",
            flush=True,
        )
        if snapshot["failures"]:
            print(f"有 {len(snapshot['failures'])} 个来源暂不可用，其余来源及72小时缓存已正常参与。", flush=True)
        return 0
    except Exception as exc:
        detail = traceback.format_exc()
        write_log(f"更新失败：{type(exc).__name__}: {exc}\n{detail}")
        print(f"\n更新失败：{exc}", file=sys.stderr, flush=True)
        print(detail, file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
