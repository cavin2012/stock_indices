(function () {
  "use strict";

  const data = window.INDEX_DATA;
  const $ = (selector) => document.querySelector(selector);
  const escapeHtml = (value) => String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");

  const palette = {
    composite: "#bddc6d",
    baoma: "#58b99b",
    novice: "#efa946",
    retail: "#de684e"
  };

  const historyState = {
    range: "60",
    mode: "raw",
    days: [],
    activeIndex: null,
    plot: null,
    interactionInstalled: false
  };

  const metricHelp = {
    composite: "社区整体情绪温度。越高表示讨论越活跃，乐观、追涨或高强度表达越明显。",
    baoma: "一种家庭资金/稳健大众表达视角，不识别作者真实身份；观察乐观、追涨与扩散。",
    novice: "一种新手式表达视角，不识别作者真实经验；重点观察求教、跟随热点与犹豫入场。",
    retail: "一种主动交易式大众表达视角，不识别作者账户类型；观察方向、强度和参与活跃。",
    optimism: "表示社区整体偏多还是偏空；50附近为中性，高于50偏多，低于50偏空。",
    direction: "表示社区整体偏多还是偏空；50附近为中性，高于50偏多，低于50偏空。",
    fomo: "表示怕错过行情、想追涨上车的冲动强弱。",
    panic: "表示割肉、清仓、担忧下跌等恐慌情绪的强弱；越高越恐慌。",
    newcomer: "表示新手求教、询问买卖和解套问题的活跃程度。",
    intensity: "表示社区措辞是否激烈、情绪是否趋向极端。",
    attention: "表示有效帖子数量和独立参与者在同平台、同交易日中的活跃位置；点赞、评论累计量不参与正式分数。",
    heat: "板块热度 V6 先在每个渠道内部校准，再让东方财富、新浪、同花顺、淘股吧各占 25%，最后合成最近三个交易日。热度高不等于看多。",
    relativeHeat: "表示最近交易日30个细分板块之间的相对关注位置；只有达到覆盖和稳定性门槛的板块才给正式名次。",
    absoluteHeat: "表示该板块最近三个交易日自身的帖子数量与参与温度，适合与它过去的交易日数值比较。",
    legacyHeat: "表示追涨、新手提问、激烈措辞和讨论活跃共同形成的情绪躁动度；它不是纯发帖量。",
    activityIndex: "表示同平台、同交易日内由有效帖子数量和独立参与者支撑的关注活跃位置；同花顺热榜不参与声量排名。",
    breadth: "表示升温是否扩散到多个板块，而不是只集中在少数热点。",
    sample: "表示参与当前结果的有效社区文本数量。",
    coverage: "表示当前成功覆盖的渠道数和社区来源数；缺失渠道保留中性席位，不把权重转给其它渠道。",
    confidence: "表示本次成功读取的数据来源完整程度，不代表预测准确率。"
  };

  function helpBadge(key) {
    const meaning = metricHelp[key];
    if (!meaning) return "";
    return `<span class="metric-help" tabindex="0" role="note" title="${escapeHtml(meaning)}" data-help="${escapeHtml(meaning)}" aria-label="${escapeHtml(meaning)}">i</span>`;
  }

  function installMetricHelp() {
    const tip = document.createElement("div");
    tip.className = "metric-tooltip";
    tip.setAttribute("role", "tooltip");
    document.body.appendChild(tip);
    let active = null;
    let pinned = null;

    function show(target) {
      const meaning = target.dataset.help || target.getAttribute("aria-label") || target.title;
      if (!meaning) return;
      active = target;
      tip.textContent = meaning;
      tip.classList.add("show");
      const rect = target.getBoundingClientRect();
      const left = Math.max(12, Math.min(window.innerWidth - tip.offsetWidth - 12, rect.left + rect.width / 2 - tip.offsetWidth / 2));
      let top = rect.top - tip.offsetHeight - 10;
      if (top < 10) top = rect.bottom + 10;
      tip.style.left = `${left}px`;
      tip.style.top = `${top}px`;
    }

    function hide() {
      active = null;
      tip.classList.remove("show");
    }

    function clear() {
      pinned = null;
      hide();
    }

    document.addEventListener("mouseover", (event) => {
      const target = event.target.closest?.(".metric-help");
      if (target && !pinned) show(target);
    });
    document.addEventListener("mouseout", (event) => {
      if (!pinned && active && !event.relatedTarget?.closest?.(".metric-help")) hide();
    });
    document.addEventListener("focusin", (event) => {
      const target = event.target.closest?.(".metric-help");
      if (target && !pinned) show(target);
    });
    document.addEventListener("focusout", (event) => {
      if (!pinned && event.target.closest?.(".metric-help")) hide();
    });
    document.addEventListener("click", (event) => {
      const target = event.target.closest?.(".metric-help");
      if (!target) {
        clear();
        return;
      }
      event.preventDefault();
      event.stopPropagation();
      if (pinned === target) clear();
      else {
        pinned = target;
        show(target);
      }
    });
    window.addEventListener("scroll", clear, { passive: true });
    window.addEventListener("resize", clear);
  }

  const indexMeta = {
    baoma: {
      kicker: "SOCIAL FOMO",
      name: "宝妈指数",
      copy: "一种家庭资金/稳健大众表达视角：观察乐观扩散、追涨语言和讨论出圈程度，不识别作者真实身份。",
      foot: "追涨与乐观权重更高"
    },
    novice: {
      kicker: "NEWCOMER VOICE",
      name: "小白指数",
      copy: "一种新手式表达视角：强调“能买吗、怎么操作、求教”等语言，不识别作者真实投资年限。",
      foot: "新手提问权重更高"
    },
    retail: {
      kicker: "RETAIL MOOD",
      name: "散户指数",
      copy: "一种主动交易式大众表达视角：观察看多看空、情绪强度和讨论活跃，不识别真实账户类型。",
      foot: "讨论方向与活跃度更高"
    }
  };

  const signalMeta = {
    optimism: ["情绪方向", "表示讨论整体偏多还是偏空。50 附近为中性，高于 50 偏多，低于 50 偏空。"],
    fomo: ["追涨冲动", "表示大家怕错过行情、想追涨或赶紧上车的冲动。"],
    panic: ["恐慌风险", "表示割肉、清仓、担忧下跌等恐慌情绪的强弱。"],
    newcomer: ["新手提问", "表示新手求教、询问买卖和解套问题的活跃程度。"],
    intensity: ["情绪强度", "表示社区措辞是否激烈、情绪是否趋向极端。"],
    attention: ["讨论活跃", "表示有效帖子数量和独立参与者在同平台、同交易日中的活跃位置；不使用点赞、评论累计量。"]
  };

  function levelFor(score) {
    if (score >= 80) return "极热";
    if (score >= 65) return "偏热";
    if (score >= 50) return "活跃";
    if (score >= 35) return "偏冷";
    return "冷清";
  }

  function colorFor(score) {
    if (score >= 80) return "#b83e3e";
    if (score >= 65) return "#de684e";
    if (score >= 50) return "#efa946";
    if (score >= 35) return "#2d9278";
    return "#647d77";
  }

  function isTradingHistoryPoint(point) {
    const explicit = point?.isTradingDay ?? point?.tradingDay ?? point?.calendar?.isTradingDay;
    if (explicit === false) return false;
    if (explicit === true) return true;
    const date = String(point?.date || "");
    if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) return true;
    const weekday = new Date(`${date}T00:00:00Z`).getUTCDay();
    return weekday !== 0 && weekday !== 6;
  }

  function previousPoint(history) {
    const points = (Array.isArray(history) ? history : []).filter(isTradingHistoryPoint);
    if (points.length < 2) return null;
    return points[points.length - 2];
  }

  function deltaText(current, previous) {
    if (previous === null || previous === undefined || !Number.isFinite(Number(previous))) return "社区文本版首个记录点";
    const delta = Number(current) - Number(previous);
    if (delta === 0) return "较上一交易日持平";
    return `较上一交易日 ${delta > 0 ? "+" : ""}${delta}`;
  }

  function formatDateTime(value) {
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value || "--");
    return date.toLocaleString("zh-CN", { hour12: false });
  }

  function formatPostTime(value) {
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "时间未知";
    return date.toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false });
  }

  function renderHeader(snapshot) {
    const score = Number(snapshot.scores?.composite || 0);
    const previous = previousPoint(snapshot.history || []);
    $("#market-date").textContent = `最近交易日 ${snapshot.marketDate || "--"}`;
    $("#updated-at").textContent = formatDateTime(snapshot.updatedAt);
    $("#confidence-text").innerHTML = `社区源完整度 ${snapshot.confidence ?? 0}% ${helpBadge("confidence")}`;
    const dot = $("#confidence-dot");
    dot.classList.remove("ok", "warn");
    dot.classList.add(Number(snapshot.confidence) >= 80 ? "ok" : "warn");
    $("#composite-score").textContent = score;
    $("#composite-level").textContent = levelFor(score);
    const heat = Number(snapshot.heat ?? snapshot.signals?.heat ?? 0);
    const direction = Number(snapshot.direction ?? snapshot.signals?.optimism ?? 0);
    $("#composite-delta").textContent = `${deltaText(score, previous?.composite)} · 热度 ${heat.toFixed(1)} · 方向 ${direction.toFixed(1)}`;
    const ring = $("#composite-ring");
    ring.style.setProperty("--score", String(score));
    ring.style.setProperty("--ring-color", colorFor(score));
  }

  function renderHeatSnapshot(snapshot) {
    const target = $("#heat-snapshot");
    if (!target) return;
    const sectors = [...(snapshot.sectorIndices || [])]
      .filter((item) => item.available !== false && item.metrics?.fairness?.rankEligible === true && item.heatRank)
      .sort((a, b) => Number(a.heatRank || 999) - Number(b.heatRank || 999));
    if (!sectors.length) {
      target.innerHTML = `<div class="heat-snapshot-loading">板块试算值已经保留，但目前通过公平性门槛的板块不足 5 个，因此暂不宣布“热度首位”。后续更新会自动恢复正式排名。</div>`;
      return;
    }
    const first = sectors[0];
    const last = sectors.at(-1);
    const range = Number(first.heat || 0) - Number(last?.heat || 0);
    target.innerHTML = `
      <div class="heat-snapshot-head">
        <div><span class="heat-v2-badge">板块热度 V6 · 四渠道等权</span><h2>最近交易日社区热度首位：${escapeHtml(first.name)} <strong>${Number(first.heat || 0).toFixed(1)}</strong></h2></div>
        <p>当前板块跨度 <strong>${range.toFixed(1)}</strong> · 市场热度 <strong>${Number(snapshot.heat || 0).toFixed(1)}</strong></p>
      </div>
      <div class="heat-snapshot-list">${sectors.slice(0, 6).map((item, index) => `
        <div class="heat-snapshot-item">
          <span>${String(index + 1).padStart(2, "0")}</span>
          <strong>${escapeHtml(item.name)}</strong>
          <b>${Number(item.heat || 0).toFixed(1)}</b>
          <small>相对 ${Number(item.relativeHeat ?? 50).toFixed(1)}</small>
        </div>`).join("")}
      </div>
      <p class="heat-snapshot-note">V6 固定东方财富、新浪、同花顺板块社区、淘股吧各占 25%，渠道缺失保持中性且不转权，再按 50% / 30% / 20% 合成；只有通过覆盖、样本与稳定性门槛的板块进入榜单。</p>`;
  }

  function renderIndexCards(snapshot) {
    const previous = previousPoint(snapshot.history || []);
    $("#index-cards").innerHTML = ["baoma", "novice", "retail"].map((key) => {
      const meta = indexMeta[key];
      const score = Number(snapshot.scores?.[key] || 0);
      return `<article class="index-card">
        <div class="card-top">
          <div><span class="card-kicker">${meta.kicker}</span><h3>${meta.name} ${helpBadge(key)}</h3></div>
          <span class="mini-level">${levelFor(score)}</span>
        </div>
        <div class="card-score-row"><strong>${score}</strong><span>/ 100</span></div>
        <div class="card-meter" aria-label="${meta.name} ${score}分"><i style="width:${score}%"></i></div>
        <p class="card-copy">${meta.copy}</p>
        <div class="card-foot"><span>${meta.foot}</span><strong>${deltaText(score, previous?.[key])}</strong></div>
      </article>`;
    }).join("");
  }

  function finiteNumber(value) {
    if (value === null || value === undefined || value === "") return null;
    const number = Number(value);
    return Number.isFinite(number) ? number : null;
  }

  function tradingDayPostStats(metrics = {}, fallbackTotal = null) {
    const labels = ["最近交易日", "上一交易日", "前两个交易日"];
    const rows = [];
    const raw = metrics.dailyPostCounts ?? metrics.tradingDayPostCounts;
    const addRow = (value, fallbackDate = "") => {
      if (value && typeof value === "object" && !Array.isArray(value)) {
        const count = finiteNumber(value.count ?? value.posts ?? value.postCount ?? value.samplePosts ?? value.value);
        const weight = finiteNumber(value.effectiveWeight ?? value.weight ?? value.dayWeight);
        if (count !== null) rows.push({ date: String(value.date ?? value.tradingDate ?? value.day ?? fallbackDate ?? ""), count, weight });
        return;
      }
      const count = finiteNumber(value);
      if (count !== null) rows.push({ date: String(fallbackDate || ""), count, weight: null });
    };

    if (Array.isArray(raw)) {
      raw.forEach((value) => addRow(value));
    } else if (raw && typeof raw === "object") {
      Object.entries(raw).forEach(([date, value]) => addRow(value, date));
    }
    if (rows.some((row) => /^\d{4}-\d{2}-\d{2}$/.test(row.date))) {
      rows.sort((a, b) => String(b.date).localeCompare(String(a.date)));
    }

    const explicit = [
      finiteNumber(metrics.todayPosts ?? metrics.currentTradingDayPosts),
      finiteNumber(metrics.previousTradingDayPosts),
      finiteNumber(metrics.twoTradingDaysAgoPosts)
    ];
    const daily = labels.map((label, index) => {
      const existing = rows[index];
      const count = explicit[index] ?? existing?.count ?? null;
      return { label, date: existing?.date || "", count, weight: existing?.weight ?? null };
    });
    const explicitTotal = finiteNumber(
      metrics.threeTradingDaySamplePosts ?? metrics.tradingDaySamplePosts ?? metrics.effectiveTradingDayPosts ?? metrics.tradingWindowPosts
    );
    const hasAllDailyCounts = daily.every((item) => item.count !== null);
    const total = explicitTotal ?? (hasAllDailyCounts
      ? daily.reduce((sum, item) => sum + item.count, 0)
      : finiteNumber(metrics.samplePosts ?? metrics.allSamplePosts ?? fallbackTotal));
    const effectiveWeightTotal = daily.reduce((sum, item) => sum + (item.weight ?? 0), 0);
    const weighted = finiteNumber(metrics.weightedDailyPosts ?? metrics.weightedDailyPosts3d) ?? (hasAllDailyCounts
      ? effectiveWeightTotal > 0
        ? daily.reduce((sum, item) => sum + item.count * (item.weight ?? 0), 0) / effectiveWeightTotal
        : 0.50 * daily[0].count + 0.30 * daily[1].count + 0.20 * daily[2].count
      : null);
    return { daily, total, weighted };
  }

  function tradingDayCountText(item) {
    if (!item || item.count === null) return "--";
    const date = /^\d{4}-\d{2}-\d{2}$/.test(item.date) ? `（${item.date.slice(5)}）` : "";
    return `${Number(item.count).toFixed(0)} 条${date}`;
  }

  const personaEvidenceMeta = {
    baoma: {
      label: "家庭稳健",
      kicker: "HOUSEHOLD & CAUTIOUS",
      copy: "家庭、生活资金或稳健长期型表达"
    },
    novice: {
      label: "新手提问",
      kicker: "LEARNING QUESTIONS",
      copy: "明确提问、求教或首次入市型表达"
    },
    retail: {
      label: "主动交易",
      kicker: "ACTIVE TRADING",
      copy: "明确买卖、仓位或主动交易型表达"
    }
  };

  function personaEvidenceContent(rawEvidence) {
    if (!rawEvidence || typeof rawEvidence !== "object") return "";
    const counts = rawEvidence.evidenceCounts && typeof rawEvidence.evidenceCounts === "object"
      ? rawEvidence.evidenceCounts
      : {};
    const candidates = rawEvidence.candidates && typeof rawEvidence.candidates === "object"
      ? rawEvidence.candidates
      : {};
    const keys = Object.keys(personaEvidenceMeta);
    const values = keys.map((key) => finiteNumber(counts[key] ?? candidates[key]?.evidencePosts));
    if (values.every((value) => value === null)) return "";
    const safeCounts = values.map((value) => Math.max(0, Math.round(value ?? 0)));
    const total = safeCounts.reduce((sum, value) => sum + value, 0);
    const cards = keys.map((key, index) => {
      const meta = personaEvidenceMeta[key];
      const count = safeCounts[index];
      const share = total ? count / total * 100 : 0;
      const candidate = finiteNumber(candidates[key]?.candidateShrunkDirection);
      const candidateText = candidate === null ? "候选收缩方向待样本" : `候选收缩方向 ${Math.max(0, Math.min(100, candidate)).toFixed(1)}`;
      return `<article class="persona-evidence-item is-${key}">
        <div class="persona-evidence-item-head"><div><span>${meta.kicker}</span><strong>${meta.label}</strong></div><b>${count}<small> 条</small></b></div>
        <div class="persona-evidence-meter" role="img" aria-label="${meta.label}表达证据 ${count} 条，占三类互斥证据 ${share.toFixed(0)}%"><i style="width:${share}%"></i></div>
        <p>${meta.copy}</p><small>${candidateText}</small>
      </article>`;
    }).join("");
    return `<div class="persona-evidence-grid" role="group" aria-label="三类互斥表达风格证据，共 ${total} 条">${cards}</div>`;
  }

  function personaEvidenceDisclosure(rawEvidence, titleId, sectorName = "") {
    const content = personaEvidenceContent(rawEvidence);
    if (!content) return "";
    const subject = sectorName ? `${escapeHtml(sectorName)}板块的` : "本次全市场";
    return `<div class="persona-evidence-head">
        <div><span>EXPERIMENTAL EXPRESSION EVIDENCE</span><h3 id="${titleId}">表达风格证据</h3></div>
        <p>${subject}公开讨论按互斥规则归入三种表达风格，仅用于解释样本构成。</p>
      </div>
      ${content}
      <p class="persona-evidence-caution"><strong>怎么理解：</strong>这是文本表达风格证据，不识别作者的真实身份、性别、年龄或家庭情况。实验候选收缩方向尚未改变正式宝妈、小白、散户指数。</p>`;
  }

  function renderPersonaEvidenceOverview(snapshot) {
    const shell = $("#persona-evidence-overview");
    if (!shell) return;
    const html = personaEvidenceDisclosure(snapshot?.diagnostics?.personaEvidence, "persona-evidence-title");
    shell.innerHTML = html;
    shell.hidden = !html;
    if (html) shell.setAttribute("aria-labelledby", "persona-evidence-title");
    else shell.removeAttribute("aria-labelledby");
  }

  function percentValue(...values) {
    const raw = values.map(finiteNumber).find((value) => value !== null);
    if (raw === undefined) return null;
    const percent = raw >= 0 && raw <= 1 ? raw * 100 : raw;
    return Math.max(0, Math.min(100, percent));
  }

  function canonicalPercent(...values) {
    const raw = values.map(finiteNumber).find((value) => value !== null);
    return raw === undefined ? null : Math.max(0, Math.min(100, raw));
  }

  function historyKind(point) {
    const raw = String(
      point?.historyType ?? point?.type ?? point?.kind ?? point?.provenance ?? point?.sourceType ?? ""
    ).toLowerCase();
    if (
      point?.isEstimated === true || point?.estimated === true || point?.isEstimate === true ||
      point?.backfilled === true || point?.isBackfill === true ||
      /(estimated|estimate|backfill|proxy|synthetic|回溯|估算)/.test(raw)
    ) return "estimated";
    return "measured";
  }

  function historyScore(point, key) {
    const score = finiteNumber(point?.[key] ?? point?.scores?.[key]);
    return score === null ? null : Math.max(0, Math.min(100, score));
  }

  function historyQualityStatus(point, kind, confidence) {
    if (kind !== "estimated") return "measured";
    const explicit = String(point?.qualityStatus || "").trim().toLowerCase();
    if (explicit === "usable" || explicit === "provisional") return explicit;
    return confidence !== null && confidence >= 55 ? "usable" : "provisional";
  }

  function historyVisualClass(point) {
    if (!point) return "missing";
    return point.__quality === "provisional" ? "provisional" : point.__kind;
  }

  function normalizeHistory(history, snapshot) {
    const unique = new Map();
    (Array.isArray(history) ? history : []).forEach((point) => {
      if (!isTradingHistoryPoint(point)) return;
      const date = String(point?.date || "");
      if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) return;
      const time = Date.parse(`${date}T00:00:00Z`);
      if (!Number.isFinite(time)) return;
      const isLatest = date === String(snapshot?.marketDate || "");
      const coverage = canonicalPercent(
        point.coverage,
        point.coverageDetails?.score
      ) ?? percentValue(
        point.coveragePct,
        point.coverageScore,
        point.dataCoverage,
        point.quality?.coverage
      ) ?? (isLatest ? canonicalPercent(snapshot?.confidence) : null);
      const confidence = canonicalPercent(
        point.confidence,
        point.confidenceDetails?.score
      ) ?? percentValue(
        point.confidencePct,
        point.confidenceScore,
        point.dataConfidence,
        point.quality?.confidence
      ) ?? (isLatest ? canonicalPercent(snapshot?.confidence) : null);
      const kind = historyKind(point);
      const methodology = String(point.methodology || "legacy_unspecified");
      const normalized = {
        ...point,
        date,
        __time: time,
        __kind: kind,
        __quality: historyQualityStatus(point, kind, confidence),
        __methodology: methodology,
        __coverage: coverage,
        __confidence: confidence,
        __samplePosts: finiteNumber(point.samplePosts ?? point.sampleCount ?? point.allSamplePosts ?? point.diagnostics?.allSamplePosts),
        __platformCount: finiteNumber(point.platformCount ?? point.diagnostics?.platformCount),
        __sourceCount: finiteNumber(point.sourceCount ?? point.diagnostics?.sourceCount)
      };
      ["composite", "baoma", "novice", "retail", "heat", "direction"].forEach((key) => {
        normalized[key] = historyScore(point, key);
      });
      if (["composite", "baoma", "novice", "retail"].some((key) => normalized[key] !== null)) {
        const previous = unique.get(date);
        const kindRank = { estimated: 0, measured: 1 };
        if (!previous || kindRank[normalized.__kind] >= kindRank[previous.__kind]) unique.set(date, normalized);
      }
    });
    return [...unique.values()].sort((a, b) => a.__time - b.__time);
  }

  function dateKey(time) {
    return new Date(time).toISOString().slice(0, 10);
  }

  function historyWindow(points, range) {
    const endTime = points.at(-1)?.__time;
    if (!Number.isFinite(endTime)) return [];
    if (range === "half-year") {
      const start = new Date(endTime);
      start.setUTCMonth(start.getUTCMonth() - 6);
      return points
        .filter((point) => point.__time >= start.getTime())
        .map((point) => ({ date: point.date, time: point.__time, point }));
    }
    return points.slice(-60).map((point) => ({ date: point.date, time: point.__time, point }));
  }

  function smoothedHistoryValue(days, index, key) {
    const values = [];
    for (let cursor = index; cursor >= Math.max(0, index - 6); cursor -= 1) {
      const point = days[cursor]?.point;
      const value = point?.[key];
      if (
        !point || value === null || value === undefined ||
        point.__quality === "provisional" ||
        point.__confidence === null || point.__confidence < 55
      ) break;
      values.unshift(Number(value));
    }
    if (values.length < 3) return null;
    return values.reduce((sum, value) => sum + value, 0) / values.length;
  }

  function plottedHistoryValue(days, index, key, mode) {
    if (mode === "smooth") return smoothedHistoryValue(days, index, key);
    return days[index]?.point?.[key] ?? null;
  }

  function averagePercent(values) {
    const valid = values.filter((value) => value !== null && Number.isFinite(value));
    return valid.length ? valid.reduce((sum, value) => sum + value, 0) / valid.length : null;
  }

  function qualityTone(value) {
    if (value === null) return "is-unknown";
    if (value >= 80) return "is-good";
    if (value >= 55) return "is-medium";
    return "is-low";
  }

  function renderHistorySummary(days) {
    const recorded = days.map((day) => day.point).filter(Boolean);
    const measured = recorded.filter((point) => point.__kind === "measured").length;
    const estimated = recorded.filter((point) => point.__kind === "estimated").length;
    const provisional = recorded.filter((point) => point.__quality === "provisional").length;
    const usable = Math.max(0, estimated - provisional);
    const missing = Math.max(0, days.length - recorded.length);
    const coverage = averagePercent(recorded.map((point) => point.__coverage));
    const confidence = averagePercent(recorded.map((point) => point.__confidence));
    const summary = $("#history-summary");
    if (!summary) return { recorded, measured, estimated, usable, provisional, missing, coverage, confidence };
    const items = [
      ["交易日实测", `${measured} 个`, "由交易日更新留下的真实社区快照", "is-measured"],
      ["可用回溯", `${usable} 个`, "档案、置信度与近7个交易日发布平台均达标", "is-estimated"],
      ["低置信回溯 / 待完善", `${provisional} 个`, "仍可查看原始点，但待更多历史样本完善", "is-provisional"],
      ["平均覆盖度", coverage === null ? "--" : `${coverage.toFixed(0)}%`, "该时段可用平台、来源和样本的完整程度", qualityTone(coverage)],
      ["平均置信度", confidence === null ? "--" : `${confidence.toFixed(0)}%`, "数据对交易日社区状态的可参考程度，不是预测胜率", qualityTone(confidence)]
    ];
    summary.innerHTML = items.map(([label, value, copy, tone]) => `<article class="history-summary-card ${tone}">
      <span>${label}</span><strong>${value}</strong><small>${copy}</small>
    </article>`).join("");
    return { recorded, measured, estimated, usable, provisional, missing, coverage, confidence };
  }

  function formatHistoryDay(date) {
    const parsed = new Date(`${date}T00:00:00Z`);
    if (Number.isNaN(parsed.getTime())) return date;
    return parsed.toLocaleDateString("zh-CN", { timeZone: "UTC", year: "numeric", month: "long", day: "numeric", weekday: "short" });
  }

  function historyStatusLabel(point) {
    if (!point) return "缺失";
    if (point.__quality === "provisional") return "低置信回溯 / 待完善";
    return point.__kind === "estimated" ? "可用回溯" : "交易日实测";
  }

  function displayHistoryValue(value, digits = 1) {
    return value === null || !Number.isFinite(value) ? "--" : Number(value).toFixed(digits);
  }

  function renderHistoryTable(days) {
    const wrap = $("#history-table-wrap");
    if (!wrap) return;
    if (!days.length) {
      wrap.innerHTML = `<p class="history-table-empty">还没有可展示的历史记录。</p>`;
      return;
    }
    const rows = [...days].reverse().map((day) => {
      const point = day.point;
      const visualClass = historyVisualClass(point);
      return `<tr class="is-${visualClass}">
        <th scope="row">${escapeHtml(day.date)}</th>
        <td><span class="history-table-status is-${visualClass}">${historyStatusLabel(point)}</span></td>
        <td>${displayHistoryValue(point?.composite)}</td>
        <td>${displayHistoryValue(point?.baoma)}</td>
        <td>${displayHistoryValue(point?.novice)}</td>
        <td>${displayHistoryValue(point?.retail)}</td>
        <td>${point?.__coverage === null || !point ? "--" : `${point.__coverage.toFixed(0)}%`}</td>
        <td>${point?.__confidence === null || !point ? "--" : `${point.__confidence.toFixed(0)}%`}</td>
      </tr>`;
    }).join("");
    wrap.innerHTML = `<table class="history-table">
      <caption>当前图表范围内的交易日指数、数据类型、覆盖度与置信度</caption>
      <thead><tr><th scope="col">日期</th><th scope="col">类型</th><th scope="col">总指数</th><th scope="col">宝妈</th><th scope="col">小白</th><th scope="col">散户</th><th scope="col">覆盖度</th><th scope="col">置信度</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>`;
  }

  function historyTooltipHtml(day, index) {
    const point = day?.point;
    if (!point) {
      return `<div class="history-tooltip-head"><strong>${escapeHtml(formatHistoryDay(day.date))}</strong><span class="is-missing">缺失</span></div>
        <p class="history-tooltip-empty">该交易日没有足够数据，因此不生成虚构点。</p>`;
    }
    const quality = [
      ["覆盖度", point.__coverage === null ? "--" : `${point.__coverage.toFixed(0)}%`],
      ["置信度", point.__confidence === null ? "--" : `${point.__confidence.toFixed(0)}%`],
      ["样本", point.__samplePosts === null ? "--" : `${Math.round(point.__samplePosts)} 条`],
      ["平台 / 来源", point.__platformCount === null && point.__sourceCount === null ? "--" : `${point.__platformCount ?? "--"} / ${point.__sourceCount ?? "--"}`]
    ];
    const smoothValues = historyState.mode === "smooth"
      ? [["总", "composite"], ["宝妈", "baoma"], ["小白", "novice"], ["散户", "retail"]]
        .map(([label, key]) => `${label} ${displayHistoryValue(smoothedHistoryValue(historyState.days, index, key))}`).join(" · ")
      : "";
    const provisionalWarning = point.__quality === "provisional"
      ? `<p class="history-tooltip-warning">该回溯点的档案完整度、置信度或近 7 个交易日发布平台尚未全部达标，当前标记为待完善，不参与 7 交易日平滑。</p>`
      : "";
    return `<div class="history-tooltip-head"><strong>${escapeHtml(formatHistoryDay(day.date))}</strong><span class="is-${historyVisualClass(point)}">${historyStatusLabel(point)}</span></div>
      <div class="history-tooltip-scores">
        <span><i style="background:${palette.composite}"></i>总指数 <strong>${displayHistoryValue(point.composite)}</strong></span>
        <span><i style="background:${palette.baoma}"></i>宝妈 <strong>${displayHistoryValue(point.baoma)}</strong></span>
        <span><i style="background:${palette.novice}"></i>小白 <strong>${displayHistoryValue(point.novice)}</strong></span>
        <span><i style="background:${palette.retail}"></i>散户 <strong>${displayHistoryValue(point.retail)}</strong></span>
      </div>${provisionalWarning}${smoothValues ? `<p class="history-tooltip-smooth"><span>7 交易日平滑线</span>${smoothValues}</p>` : ""}
      <div class="history-tooltip-quality">${quality.map(([label, value]) => `<span>${label}<strong>${value}</strong></span>`).join("")}</div>`;
  }

  function historyStatusText(day, index) {
    if (!day?.point) return `${formatHistoryDay(day.date)}，该交易日数据缺失。`;
    const point = day.point;
    const smoothText = historyState.mode === "smooth"
      ? `，7交易日平滑总指数 ${displayHistoryValue(smoothedHistoryValue(historyState.days, index, "composite"))}`
      : "";
    return `${formatHistoryDay(day.date)}，${historyStatusLabel(point)}，总指数 ${displayHistoryValue(point.composite)}，宝妈 ${displayHistoryValue(point.baoma)}，小白 ${displayHistoryValue(point.novice)}，散户 ${displayHistoryValue(point.retail)}，覆盖度 ${point.__coverage === null ? "未知" : `${point.__coverage.toFixed(0)}%`}，置信度 ${point.__confidence === null ? "未知" : `${point.__confidence.toFixed(0)}%`}${smoothText}。`;
  }

  function setActiveHistoryDay(index, announce = false) {
    const svg = $("#history-chart");
    const tooltip = $("#history-tooltip");
    const wrap = $("#history-chart-wrap");
    const stage = $("#history-chart-stage");
    const status = $("#history-chart-status");
    const day = historyState.days[index];
    const plot = historyState.plot;
    if (!svg || !tooltip || !wrap || !stage || !day || !plot) return;
    historyState.activeIndex = index;
    const crosshair = svg.querySelector("#history-crosshair");
    const x = plot.x(index);
    if (crosshair) {
      crosshair.setAttribute("x1", String(x));
      crosshair.setAttribute("x2", String(x));
      crosshair.classList.add("show");
    }
    svg.querySelectorAll(".chart-point.active").forEach((node) => node.classList.remove("active"));
    svg.querySelectorAll(`[data-history-index="${index}"].chart-point`).forEach((node) => node.classList.add("active"));
    tooltip.innerHTML = historyTooltipHtml(day, index);
    tooltip.hidden = false;
    const svgRect = svg.getBoundingClientRect();
    const stageRect = stage.getBoundingClientRect();
    const visibleWidth = stage.clientWidth;
    const xPixel = svgRect.left - stageRect.left + (x / plot.width) * svgRect.width;
    const tooltipHalf = Math.min(150, Math.max(118, visibleWidth * 0.32));
    const visibleLeft = tooltipHalf + 8;
    const visibleRight = visibleWidth - tooltipHalf - 8;
    const clamped = Math.max(visibleLeft, Math.min(visibleRight, xPixel));
    tooltip.style.left = `${clamped}px`;
    if (announce && status) status.textContent = historyStatusText(day, index);
  }

  function hideHistoryTooltip() {
    const svg = $("#history-chart");
    const tooltip = $("#history-tooltip");
    if (tooltip) tooltip.hidden = true;
    svg?.querySelector("#history-crosshair")?.classList.remove("show");
    svg?.querySelectorAll(".chart-point.active").forEach((node) => node.classList.remove("active"));
  }

  function historyIndexFromPointer(event) {
    const svg = $("#history-chart");
    const plot = historyState.plot;
    if (!svg || !plot || !historyState.days.length) return null;
    const rect = svg.getBoundingClientRect();
    const svgX = ((event.clientX - rect.left) / rect.width) * plot.width;
    const raw = plot.step > 0 ? Math.round((svgX - plot.left) / plot.step) : 0;
    return Math.max(0, Math.min(historyState.days.length - 1, raw));
  }

  function installHistoryInteraction() {
    if (historyState.interactionInstalled) return;
    const svg = $("#history-chart");
    if (!svg) return;
    historyState.interactionInstalled = true;
    svg.addEventListener("pointermove", (event) => {
      const index = historyIndexFromPointer(event);
      if (index !== null) setActiveHistoryDay(index, false);
    });
    svg.addEventListener("pointerdown", (event) => {
      const index = historyIndexFromPointer(event);
      if (index !== null) setActiveHistoryDay(index, true);
    });
    svg.addEventListener("pointerleave", () => {
      if (document.activeElement !== svg) hideHistoryTooltip();
    });
    svg.addEventListener("focus", () => {
      if (!historyState.days.length) return;
      const fallback = historyState.days.length - 1;
      setActiveHistoryDay(historyState.activeIndex ?? fallback, true);
    });
    svg.addEventListener("blur", hideHistoryTooltip);
    svg.addEventListener("keydown", (event) => {
      if (!historyState.days.length) return;
      let next = historyState.activeIndex ?? historyState.days.length - 1;
      if (event.key === "ArrowLeft") next -= 1;
      else if (event.key === "ArrowRight") next += 1;
      else if (event.key === "Home") next = 0;
      else if (event.key === "End") next = historyState.days.length - 1;
      else if (event.key === "Escape") {
        hideHistoryTooltip();
        return;
      } else return;
      event.preventDefault();
      setActiveHistoryDay(Math.max(0, Math.min(historyState.days.length - 1, next)), true);
    });
  }

  function renderChart(history, range = historyState.range, snapshot = data) {
    const svg = $("#history-chart");
    const legend = $("#chart-legend");
    if (!svg || !legend) return;
    historyState.range = range;
    const mode = historyState.mode;
    historyState.activeIndex = null;
    hideHistoryTooltip();
    const points = normalizeHistory(history, snapshot);
    const days = historyWindow(points, range);
    historyState.days = days;
    const summary = renderHistorySummary(days);
    renderHistoryTable(days);
    const series = [["composite", "总指数"], ["baoma", "宝妈"], ["novice", "小白"], ["retail", "散户"]];
    legend.innerHTML = series.map(([key, name]) => `<span><i style="background:${palette[key]}"></i>${name} ${helpBadge(key)}</span>`).join("");
    if (days.length) {
      const windowName = range === "half-year" ? "最近半年交易日" : "最近 60 个交易日";
      const modeText = mode === "smooth" ? " · 当前为 7 交易日平滑线，原始交易日值仍以圆点保留" : " · 当前为原始交易日值";
      const methodologyCount = new Set(days.map((day) => day.point?.__methodology).filter(Boolean)).size;
      const methodologyText = methodologyCount > 1 ? " · 存在方法断点，折线已断开且断点两侧不直接横比" : "";
      $("#history-note").textContent = `${windowName} · ${days[0].date.slice(5)} 至 ${days.at(-1).date.slice(5)} · 实测 ${summary.measured} 个 · 可用回溯 ${summary.usable} 个 · 低置信回溯待完善 ${summary.provisional} 个${methodologyText}${modeText}`;
    } else {
      $("#history-note").textContent = "暂无交易日历史记录；首个交易日更新后将自动追加";
    }

    const width = 980;
    const height = 360;
    const left = 54;
    const right = 24;
    const top = 24;
    const bottom = 52;
    const plotW = width - left - right;
    const plotH = height - top - bottom;
    const step = days.length > 1 ? plotW / (days.length - 1) : 0;
    const x = (index) => left + (days.length <= 1 ? plotW / 2 : index * step);
    const y = (score) => top + plotH - (Number(score) / 100) * plotH;
    historyState.plot = { width, height, left, right, top, bottom, plotW, plotH, step, x, y };
    const grid = [0, 25, 50, 75, 100].map((tick) =>
      `<line class="chart-grid${tick === 50 ? " is-neutral" : ""}" x1="${left}" x2="${width - right}" y1="${y(tick)}" y2="${y(tick)}"></line>
       <text class="chart-label" x="${left - 12}" y="${y(tick) + 4}" text-anchor="end">${tick}</text>`
    ).join("");
    if (!days.length) {
      svg.innerHTML = grid + `<text class="chart-empty" x="${width / 2}" y="${height / 2}">双击“一键更新指数.cmd”生成第一个记录点</text>`;
      svg.setAttribute("aria-label", "指数历史趋势图，当前没有历史记录");
      installHistoryInteraction();
      return;
    }

    const lines = series.map(([key]) => {
      const color = palette[key];
      const segments = [];
      for (let index = 1; index < days.length; index += 1) {
        const previous = days[index - 1].point;
        const current = days[index].point;
        const previousValue = plottedHistoryValue(days, index - 1, key, mode);
        const currentValue = plottedHistoryValue(days, index, key, mode);
        if (previousValue === null || previousValue === undefined || currentValue === null || currentValue === undefined) continue;
        if (previous.__methodology !== current.__methodology) continue;
        const provisional = previous.__quality === "provisional" || current.__quality === "provisional";
        const estimated = previous.__kind === "estimated" || current.__kind === "estimated";
        const visualClass = provisional ? "is-provisional" : estimated ? "is-estimated" : "is-measured";
        segments.push(`<line class="chart-line ${visualClass}${mode === "smooth" ? " is-smoothed" : ""}" x1="${x(index - 1)}" y1="${y(previousValue)}" x2="${x(index)}" y2="${y(currentValue)}" stroke="${color}"></line>`);
      }
      return segments.join("");
    }).join("");

    const markers = series.map(([key, name]) => days.map((day, index) => {
      const point = day.point;
      const value = point?.[key];
      if (value === null || value === undefined) return "";
      const estimated = point.__kind === "estimated";
      const visualClass = historyVisualClass(point);
      const coverage = point.__coverage === null ? "覆盖度未知" : `覆盖度 ${point.__coverage.toFixed(0)}%`;
      const confidence = point.__confidence === null ? "置信度未知" : `置信度 ${point.__confidence.toFixed(0)}%`;
      return `<circle class="chart-point is-${visualClass}${mode === "smooth" ? " is-raw-context" : ""}" data-history-index="${index}" cx="${x(index)}" cy="${y(value)}" r="${estimated ? 2.8 : 3.2}" fill="${estimated ? "#172e28" : palette[key]}" stroke="${palette[key]}"><title>${day.date} · ${historyStatusLabel(point)} · ${name}原始日值 ${Number(value).toFixed(1)} · ${coverage} · ${confidence}</title></circle>`;
    }).join("")).join("");

    const labelCount = range === "half-year" ? 6 : 5;
    const labelIndexes = [...new Set(Array.from({ length: labelCount }, (_, index) => Math.round(index * (days.length - 1) / Math.max(1, labelCount - 1))))];
    const xLabels = labelIndexes.map((index) => `<text class="chart-label" x="${x(index)}" y="${height - 15}" text-anchor="middle">${escapeHtml(days[index].date.slice(5))}</text>`).join("");
    const hitWidth = Math.max(5, days.length > 1 ? plotW / (days.length - 1) : plotW);
    const hitAreas = days.map((day, index) => `<rect class="chart-hit-area" data-history-index="${index}" x="${x(index) - hitWidth / 2}" y="${top}" width="${hitWidth}" height="${plotH}" aria-hidden="true"></rect>`).join("");
    svg.innerHTML = `${grid}${lines}${markers}<line id="history-crosshair" class="history-crosshair" y1="${top}" y2="${top + plotH}"></line>${hitAreas}${xLabels}`;
    svg.setAttribute("aria-label", `四项指数交易日历史趋势图，${days[0].date}至${days.at(-1).date}，${mode === "smooth" ? "显示7交易日平滑线并保留原始点，低置信回溯不参与平滑" : "显示原始交易日值"}，交易日实测${summary.measured}个，可用回溯${summary.usable}个，低置信回溯待完善${summary.provisional}个；不补周末或休市空点`);
    installHistoryInteraction();
  }

  function installHistoryRangeControls(history, snapshot) {
    document.querySelectorAll("[data-history-range]").forEach((button) => {
      button.addEventListener("click", () => {
        const range = button.dataset.historyRange || "60";
        document.querySelectorAll("[data-history-range]").forEach((item) => {
          const active = item === button;
          item.classList.toggle("active", active);
          item.setAttribute("aria-pressed", String(active));
        });
        renderChart(history, range, snapshot);
      });
    });
    document.querySelectorAll("[data-history-view]").forEach((button) => {
      button.addEventListener("click", () => {
        historyState.mode = button.dataset.historyView === "smooth" ? "smooth" : "raw";
        document.querySelectorAll("[data-history-view]").forEach((item) => {
          const active = item === button;
          item.classList.toggle("active", active);
          item.setAttribute("aria-pressed", String(active));
        });
        renderChart(history, historyState.range, snapshot);
      });
    });
  }

  function renderSignals(snapshot) {
    const signals = snapshot.signals || {};
    $("#signal-grid").innerHTML = Object.entries(signalMeta).map(([key, meta]) => {
      const score = Number(signals[key] || 0);
      return `<article class="signal-card">
        <div><h3>${meta[0]} ${helpBadge(key)}</h3><strong>${score.toFixed(1)}</strong></div>
        <p>${meta[1]}</p>
        <div class="signal-meter"><i style="width:${score}%;background:${colorFor(score)}"></i></div>
      </article>`;
    }).join("");
    const d = snapshot.diagnostics || {};
    const postStats = tradingDayPostStats(d, d.allSamplePosts ?? d.samplePosts);
    const items = [
      ["社区热度", `${Number(snapshot.heat ?? signals.heat ?? 0).toFixed(1)}`, "heat"],
      ["情绪方向", `${Number(snapshot.direction ?? signals.optimism ?? 0).toFixed(1)}`, "direction"],
      ["三交易日文本样本", postStats.total === null ? "--" : `${Math.round(postStats.total)} 条`, "sample"],
      ["加权日帖子量", postStats.weighted === null ? "--" : `${postStats.weighted.toFixed(1)} 条`, "sample"],
      ["平台 / 来源", `${d.platformCount ?? "--"} / ${d.sourceCount ?? "--"}`, "coverage"],
      ["偏热板块", `${d.hotSectorCount ?? "--"} / ${snapshot.sectorIndices?.length ?? 16}`, "breadth"],
      ["追涨词频", `${d.fomoRate ?? "--"}%`, "fomo"],
      ["恐慌词频", `${d.panicRate ?? "--"}%`, "panic"],
      ["新手提问", `${d.newcomerRate ?? "--"}%`, "newcomer"]
    ];
    $("#diagnostics").innerHTML = items.map(([label, value, key]) => `<span>${label} ${helpBadge(key)} <strong>${value}</strong></span>`).join("");
  }

  function sentimentClass(label) {
    if (label === "看多") return "sentiment-bull";
    if (label === "看空") return "sentiment-bear";
    return "sentiment-neutral";
  }

  function sectorIsAvailable(sector) {
    return sector?.available !== false;
  }

  function sectorQuadrant(direction, heat) {
    if (heat >= 50 && direction >= 50) return { key: "hot-bull", label: "高热偏多", copy: "讨论活跃且整体偏多" };
    if (heat >= 50) return { key: "hot-bear", label: "高热偏空", copy: "讨论活跃但整体偏空" };
    if (direction >= 50) return { key: "cool-bull", label: "低热偏多", copy: "讨论不拥挤但方向偏多" };
    return { key: "cool-bear", label: "低热偏空", copy: "讨论较冷且方向偏空" };
  }

  let sectorMapResizeObserver;

  function arrangeSectorMap(map) {
    sectorMapResizeObserver?.disconnect();
    const points = [...map.querySelectorAll(".sector-map-point")].map((button) => ({
      button, xPercent: Number(button.style.getPropertyValue("--map-x").replace("%", "")),
      yPercent: Number(button.style.getPropertyValue("--map-y").replace("%", "")),
      radius: Number(button.style.getPropertyValue("--point-size").replace("px", "")) / 2 + 5
    }));
    const lines = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    lines.classList.add("sector-map-leaders");
    lines.setAttribute("aria-hidden", "true");
    map.prepend(lines);
    const layout = () => {
      const width = map.clientWidth;
      const height = map.clientHeight;
      if (!width || !height) return;
      const nodes = points.map((point) => ({ ...point, anchorX: point.xPercent * width / 100, anchorY: point.yPercent * height / 100,
        x: point.xPercent * width / 100, y: point.yPercent * height / 100 }));
      // Pull gently toward true coordinates, then resolve circle collisions.
      for (let step = 0; step < 300; step += 1) {
        if (step < 180) nodes.forEach((node) => {
          node.x += (node.anchorX - node.x) * .012;
          node.y += (node.anchorY - node.y) * .012;
        });
        for (let a = 0; a < nodes.length; a += 1) {
          for (let b = a + 1; b < nodes.length; b += 1) {
            const first = nodes[a], second = nodes[b];
            let dx = second.x - first.x, dy = second.y - first.y;
            let distance = Math.hypot(dx, dy);
            if (distance < .01) { dx = (b % 2 ? 1 : -1); dy = 1; distance = Math.SQRT2; }
            const minimum = first.radius + second.radius;
            if (distance < minimum) {
              const shift = (minimum - distance) / 2;
              const sx = dx / distance * shift, sy = dy / distance * shift;
              first.x -= sx; first.y -= sy; second.x += sx; second.y += sy;
            }
          }
        }
        nodes.forEach((node) => {
          node.x = Math.max(node.radius + 8, Math.min(width - node.radius - 8, node.x));
          node.y = Math.max(node.radius + 8, Math.min(height - node.radius - 8, node.y));
        });
      }
      lines.setAttribute("viewBox", "0 0 " + width + " " + height);
      lines.innerHTML = nodes.map((node) => {
        node.button.style.setProperty("--map-x", (node.x / width * 100) + "%");
        node.button.style.setProperty("--map-y", (node.y / height * 100) + "%");
        return '<g><line x1="' + node.anchorX + '" y1="' + node.anchorY + '" x2="' + node.x + '" y2="' + node.y + '"/><circle cx="' + node.anchorX + '" cy="' + node.anchorY + '" r="2"/></g>';
      }).join("");
    };
    sectorMapResizeObserver = new ResizeObserver(layout);
    sectorMapResizeObserver.observe(map);
    layout();
  }

  function renderSectorMap(snapshot) {
    const map = $("#sector-map");
    const summary = $("#sector-map-summary");
    const legend = $("#sector-map-legend");
    if (!map || !summary) return;
    const sectors = Array.isArray(snapshot.sectorIndices) ? snapshot.sectorIndices.filter(sectorIsAvailable) : [];
    if (!sectors.length) {
      summary.innerHTML = "";
      if (legend) legend.innerHTML = "";
      map.innerHTML = `<p class="sector-map-empty">更新后将在这里显示板块情绪地图。</p>`;
      return;
    }
    // Sorted identities keep colors stable when snapshot ranking changes.
    const palette = ["#006E90", "#B34A00", "#6E43A3", "#087F5B", "#BD3155", "#526900", "#3759B8", "#A4268B", "#007E87", "#925C22", "#5963A6", "#D34D36", "#40822A", "#8C3C63", "#6B5928", "#1569A5", "#9562B1", "#008366", "#C23E79", "#646A35", "#384C73", "#A66A00", "#5650B0", "#327C68", "#9F4035", "#57803E", "#713A90", "#287C9B", "#A34F68", "#667024"];
    const identities = [...new Set((snapshot.sectorIndices || []).map((sector) => String(sector.key || sector.name)))].sort();
    const identityFor = (sector) => {
      const index = identities.indexOf(String(sector.key || sector.name));
      return { color: palette[index % palette.length], number: String(index + 1).padStart(2, "0") };
    };
    // Bubble area (rather than radius) scales with heat on the 0–100 scale.
    const sizeFor = (heat) => Math.sqrt(Math.max(0, Math.min(100, Number(heat))) / 100) * 54;
    const xFor = (direction) => 6 + ((Math.max(35, Math.min(65, direction)) - 35) / 30) * 88;
    const yFor = (heat) => 94 - ((Math.max(20, Math.min(90, heat)) - 20) / 70) * 88;
    const neutralY = yFor(50);
    const counts = { "hot-bull": 0, "hot-bear": 0, "cool-bull": 0, "cool-bear": 0 };
    const points = sectors.map((sector, index) => {
      const direction = Number(sector.direction ?? sector.signals?.optimism ?? 0);
      const heat = Number(sector.heat ?? sector.signals?.heat ?? 0);
      const relativeHeat = Number(sector.relativeHeat ?? 50);
      const emotionHeat = Number(sector.legacyHeat ?? sector.signals?.heat ?? 0);
      const sample = Math.max(0, Number(tradingDayPostStats(sector.metrics || {}).total || 0));
      const quadrant = sectorQuadrant(direction, heat);
      counts[quadrant.key] += 1;
      const key = String(sector.key || sector.name || index);
      const pointSize = sizeFor(heat);
      const identity = identityFor(sector);
      const labelSide = xFor(direction) > 77 ? "is-label-left" : xFor(direction) < 23 ? "is-label-right" : index % 2 ? "is-label-up" : "is-label-down";
      const title = `${sector.name}：综合热度 ${heat.toFixed(1)}，相对热度 ${relativeHeat.toFixed(1)}，情绪躁动 ${emotionHeat.toFixed(1)}，方向 ${direction.toFixed(1)}，三交易日有效样本 ${sample} 条，${quadrant.label}`;
      return `<button type="button" class="sector-map-point ${labelSide}" data-sector-map-open="${escapeHtml(key)}"
        style="--map-x:${xFor(direction).toFixed(2)}%;--map-y:${yFor(heat).toFixed(2)}%;--point-size:${pointSize.toFixed(1)}px;--point-offset:${(pointSize / 2 + 4).toFixed(1)}px;--tip-offset:${(pointSize / 2 + 22).toFixed(1)}px;--point-color:${identity.color}"
        aria-haspopup="dialog" aria-controls="sector-dialog" aria-label="${escapeHtml(title)}。点击查看详情" title="${escapeHtml(title)}">
        <i aria-hidden="true"></i><b class="sector-map-number" aria-hidden="true">${identity.number}</b><strong>${escapeHtml(sector.name)}</strong><small>热度 ${heat.toFixed(1)} · 相对 ${relativeHeat.toFixed(1)} · 方向 ${direction.toFixed(1)}</small>
      </button>`;
    }).join("");
    const quadrantMeta = [
      ["hot-bull", "高热偏多", "讨论活跃且偏多"],
      ["hot-bear", "高热偏空", "讨论活跃但偏空"],
      ["cool-bull", "低热偏多", "讨论不拥挤但偏多"],
      ["cool-bear", "低热偏空", "讨论较冷且偏空"]
    ];
    summary.innerHTML = quadrantMeta.map(([key, label, copy]) => `<article class="sector-map-summary-card is-${key}"><span>${label}</span><strong>${counts[key]} 个</strong><small>${copy}</small></article>`).join("");
    map.innerHTML = `
      <div class="sector-map-quadrant is-hot-bear" style="height:${neutralY}%"><span>高热偏空</span></div>
      <div class="sector-map-quadrant is-hot-bull" style="height:${neutralY}%"><span>高热偏多</span></div>
      <div class="sector-map-quadrant is-cool-bear" style="top:${neutralY}%;height:${100 - neutralY}%"><span>低热偏空</span></div>
      <div class="sector-map-quadrant is-cool-bull" style="top:${neutralY}%;height:${100 - neutralY}%"><span>低热偏多</span></div>
      <div class="sector-map-axis is-x" style="top:${neutralY}%"><span class="is-low">偏空 35</span><strong>情绪方向 50</strong><span class="is-high">65 偏多</span></div>
      <div class="sector-map-axis is-y"><span class="is-high">热 90</span><strong style="top:${neutralY}%">热度 50</strong><span class="is-low">20 冷</span></div>
      ${points}`;
    arrangeSectorMap(map);
    if (legend) {
      legend.innerHTML = sectors.map((sector) => {
        const identity = identityFor(sector);
        const key = String(sector.key || sector.name);
        return `<button type="button" class="sector-map-legend-item" data-sector-map-open="${escapeHtml(key)}" style="--point-color:${identity.color}" aria-haspopup="dialog" aria-controls="sector-dialog"><i aria-hidden="true"></i><span class="sector-map-legend-number">${identity.number}</span><strong>${escapeHtml(sector.name)}</strong><small>热度 ${Number(sector.heat ?? sector.signals?.heat ?? 0).toFixed(1)}</small></button>`;
      }).join("");
      const controls = [...map.closest(".sector-map-layout").querySelectorAll("[data-sector-map-open]")];
      const highlight = (key) => {
        map.classList.toggle("has-highlight", Boolean(key));
        controls.forEach((button) => button.classList.toggle("is-highlighted", button.dataset.sectorMapOpen === key));
      };
      controls.forEach((button) => {
        button.addEventListener("mouseenter", () => highlight(button.dataset.sectorMapOpen));
        button.addEventListener("mouseleave", () => highlight(null));
        button.addEventListener("focus", () => highlight(button.dataset.sectorMapOpen));
        button.addEventListener("blur", () => highlight(null));
      });
    }
  }

  const sectorViewState = { sortKey: "heat", parentKey: "all" };

  function sectorParentInfo(sector) {
    const rawKey = String(sector?.parentKey ?? "").trim();
    const rawName = String(sector?.parentName ?? "").trim();
    if (!rawKey && !rawName) return null;
    return { key: rawKey || `name:${rawName}`, name: rawName || rawKey };
  }

  function sectorRepresentativeCompanies(sector) {
    if (!Array.isArray(sector?.representativeCompanies)) return [];
    return sector.representativeCompanies.map((company) => {
      if (typeof company === "string") return { name: company.trim(), code: "", role: "" };
      return {
        name: String(company?.name ?? "").trim(),
        code: String(company?.code ?? "").trim(),
        role: String(company?.role ?? "").trim()
      };
    }).filter((company) => company.name || company.code).slice(0, 3);
  }

  function sectorParentOptions(sectors) {
    const options = new Map();
    sectors.forEach((sector) => {
      const parent = sectorParentInfo(sector);
      if (!parent) return;
      const current = options.get(parent.key);
      if (current) current.count += 1;
      else options.set(parent.key, { ...parent, count: 1 });
    });
    return [...options.values()];
  }

  function renderSectorParentFilters(sectors, activeKey) {
    const target = $("#sector-parent-filter");
    if (!target) return;
    const options = sectorParentOptions(sectors);
    target.innerHTML = `<span>大类</span>
      <button type="button" class="${activeKey === "all" ? "active" : ""}" data-sector-parent="all" aria-pressed="${String(activeKey === "all")}">全部 <small>${sectors.length}</small></button>
      ${options.map((option) => `<button type="button" class="${activeKey === option.key ? "active" : ""}" data-sector-parent="${escapeHtml(option.key)}" aria-pressed="${String(activeKey === option.key)}">${escapeHtml(option.name)} <small>${option.count}</small></button>`).join("")}`;
    target.hidden = false;
  }

  function renderSectorMatrix(snapshot, sortKey = sectorViewState.sortKey, parentKey = sectorViewState.parentKey) {
    const allSectors = [...(snapshot.sectorIndices || [])];
    const parentOptions = sectorParentOptions(allSectors);
    const validParent = parentKey === "all" || parentOptions.some((option) => option.key === parentKey) ? parentKey : "all";
    sectorViewState.sortKey = sortKey;
    sectorViewState.parentKey = validParent;
    renderSectorParentFilters(allSectors, validParent);

    const sectors = allSectors
      .filter((sector) => validParent === "all" || sectorParentInfo(sector)?.key === validParent)
      .sort((a, b) => {
        const availabilityDifference = Number(sectorIsAvailable(b)) - Number(sectorIsAvailable(a));
        if (availabilityDifference) return availabilityDifference;
        if (!sectorIsAvailable(a) && !sectorIsAvailable(b)) return String(a.name || "").localeCompare(String(b.name || ""), "zh-CN");
        const rankDifference = Number(b.metrics?.fairness?.rankEligible === true) - Number(a.metrics?.fairness?.rankEligible === true);
        if (rankDifference) return rankDifference;
        const value = (item) => sortKey === "heat" ? Number(item.heat ?? item.signals?.heat ?? 0) : Number(item.scores?.[sortKey] || 0);
        return value(b) - value(a);
      });
    const sortLabel = sortKey === "composite" ? "综合" : sortKey === "heat" ? "板块热度" : indexMeta[sortKey]?.name || sortKey;
    const activeParentName = parentOptions.find((option) => option.key === validParent)?.name;
    const sectorNoun = parentOptions.length ? "细分板块" : "常见板块";
    const availableCount = sectors.filter(sectorIsAvailable).length;
    const rankEligibleCount = sectors.filter((sector) => sector.metrics?.fairness?.rankEligible === true).length;
    const pendingCount = sectors.length - availableCount;
    $("#sector-count").textContent = sectors.length
      ? `${sectors.length}${validParent === "all" ? "" : ` / ${allSectors.length}`} 个${sectorNoun}${activeParentName ? ` · ${activeParentName}` : ""} · ${availableCount} 已评分、${rankEligibleCount} 可正式排名${pendingCount ? `、${pendingCount} 待更新` : ""} · 当前按${sortLabel}排序`
      : "该大类暂时没有板块数据";
    $("#sector-grid").innerHTML = sectors.length ? sectors.map((sector, index) => {
      const available = sectorIsAvailable(sector);
      const scores = sector.scores || {};
      const metrics = sector.metrics || {};
      const fairness = metrics.fairness || {};
      const rankEligible = fairness.rankEligible === true;
      const sources = (sector.platforms || []).map((item) => item.name).join(" · ") || "社区源暂不可用";
      const sample = sector.samplePosts?.[0];
      const sectorKey = String(sector.key || sector.name || index);
      const heat = available ? Number(sector.heat ?? sector.signals?.heat ?? 0) : null;
      const relativeHeat = available ? Number(sector.relativeHeat ?? 50) : null;
      const emotionHeat = available ? Number(sector.legacyHeat ?? sector.signals?.heat ?? 0) : null;
      const direction = available ? Number(sector.direction ?? sector.signals?.optimism ?? 0) : null;
      const scoreFor = (key) => available ? Number(scores[key] ?? 0) : null;
      const postStats = tradingDayPostStats(metrics);
      const sampleTotal = !available || postStats.total === null ? "--" : Math.round(postStats.total);
      const parent = sectorParentInfo(sector);
      const companies = sectorRepresentativeCompanies(sector);
      const description = String(sector.description ?? "").trim();
      const cardContext = parent || companies.length ? `<div class="sector-card-context">
        ${parent ? `<span class="sector-parent-tag">${escapeHtml(parent.name)}</span>` : ""}
        ${companies.length ? `<div class="sector-company-list" aria-label="代表公司">${companies.map((company) => {
          const label = company.name || company.code;
          const title = [company.name, company.code, company.role].filter(Boolean).join(" · ");
          return `<span title="${escapeHtml(title)}">${escapeHtml(label)}</span>`;
        }).join("")}</div>` : ""}
      </div>` : "";
      const unavailableNotice = available ? "" : `<div class="sector-card-status" role="note"><strong>待首次成功更新</strong><span>当前缓存没有可评分文本；下一次联网成功更新会补齐。</span></div>`;
      const fairnessNotice = available && !rankEligible ? `<div class="sector-card-status is-provisional" role="note"><strong>试算值</strong><span>渠道覆盖、有效样本或稳定性尚未达标，因此显示数值但不硬排名。</span></div>` : "";
      const rankLabel = sector.heatRank ? `RANK ${String(Number(sector.heatRank)).padStart(2, "0")}` : rankEligible ? "RANK PENDING" : "NO HARD RANK";
      return `<article class="sector-card${available ? "" : " is-unavailable"}" data-sector-key="${escapeHtml(sectorKey)}">
        ${cardContext}
        ${unavailableNotice}
        ${fairnessNotice}
        <div class="sector-card-head">
          <div class="sector-title"><span>${escapeHtml(rankLabel)}</span><h3>${escapeHtml(sector.name)}</h3></div>
          <div class="sector-composite"><strong${available ? ` style="color:${colorFor(heat)}"` : ""}>${available ? heat.toFixed(1) : "--"}</strong><span>新版热度 ${helpBadge("heat")} · ${available ? escapeHtml(sector.heatTier || levelFor(heat)) : "待更新"}</span></div>
        </div>
        ${description ? `<p class="sector-card-description"><span>板块边界</span>${escapeHtml(description)}</p>` : ""}
        <div class="sector-three">
          <div class="sector-score is-baoma"><div><span>宝妈 ${helpBadge("baoma")}</span><strong>${available ? scoreFor("baoma") : "--"}</strong></div><em><i style="width:${available ? scoreFor("baoma") : 0}%"></i></em></div>
          <div class="sector-score is-novice"><div><span>小白 ${helpBadge("novice")}</span><strong>${available ? scoreFor("novice") : "--"}</strong></div><em><i style="width:${available ? scoreFor("novice") : 0}%"></i></em></div>
          <div class="sector-score is-retail"><div><span>散户 ${helpBadge("retail")}</span><strong>${available ? scoreFor("retail") : "--"}</strong></div><em><i style="width:${available ? scoreFor("retail") : 0}%"></i></em></div>
        </div>
        <div class="sector-metrics">
          <div><span>三指数综合 ${helpBadge("composite")}</span><strong>${available ? scoreFor("composite") : "--"}</strong></div>
          <div><span>相对热度 ${helpBadge("relativeHeat")}</span><strong>${available ? relativeHeat.toFixed(1) : "--"}</strong></div>
          <div><span>情绪躁动 ${helpBadge("legacyHeat")}</span><strong>${available ? emotionHeat.toFixed(1) : "--"}</strong></div>
          <div><span>情绪方向 ${helpBadge("direction")}</span><strong>${available ? direction.toFixed(1) : "--"}</strong></div>
        </div>
        <p class="sector-foot">${available ? `热度榜 ${sector.heatRank ? `#${Number(sector.heatRank)}` : "暂不硬排名"} · ${escapeHtml(sector.heatTier || "待判断")} · 公平性置信 ${escapeHtml(fairness.confidenceLabel || "--")}（${Number(fairness.reliabilityPercent || 0).toFixed(0)}%）<br>社区：${escapeHtml(sources)}${sample ? `<span class="sector-sample-copy">样本：<span class="${sentimentClass(sample.label)}">${escapeHtml(sample.label)}</span> · ${escapeHtml(sample.title)}</span>` : ""}` : "当前缓存没有该板块可评分文本。板块定义与代表公司仍可查看；下一次联网成功更新后将自动补齐指数与信号。"}</p>
        <button type="button" class="sector-open-button" data-sector-open="${escapeHtml(sectorKey)}" aria-haspopup="dialog" aria-controls="sector-dialog" aria-label="查看${escapeHtml(sector.name)}板块详情">
          <span>${available ? "查看三大指数与六项信号" : "查看板块说明与代表公司"}</span><span aria-hidden="true">→</span>
        </button>
      </article>`;
    }).join("") : `<article class="sector-card"><div class="sector-title"><span>WAITING</span><h3>等待板块数据</h3></div><p class="sector-foot">再次双击“一键更新指数.cmd”即可重试。</p></article>`;
  }

  function signalReading(key, score) {
    const value = Number(score || 0);
    if (key === "optimism") {
      if (value >= 55) return "讨论偏多";
      if (value <= 45) return "讨论偏空";
      return "方向中性";
    }
    if (key === "panic") {
      if (value >= 65) return "风险较高";
      if (value >= 50) return "需要留意";
      if (value >= 35) return "风险温和";
      return "风险较低";
    }
    if (value >= 65) return "信号较强";
    if (value >= 50) return "中等偏强";
    if (value >= 35) return "中等偏弱";
    return "信号较弱";
  }

  function safeHttpUrl(value) {
    try {
      const url = new URL(String(value || ""));
      return url.protocol === "https:" || url.protocol === "http:" ? url.href : "";
    } catch (_) {
      return "";
    }
  }

  function sectorHistoryDays(history, snapshot, sectorKey) {
    const points = normalizeHistory(history, snapshot);
    return historyWindow(points, "60").map((day) => {
      const sectors = Array.isArray(day.point?.sectors) ? day.point.sectors : [];
      const sectorPoint = sectors.find((item) => String(item?.key || "") === String(sectorKey || ""));
      return {
        ...day,
        point: sectorPoint && sectorPoint.available !== false
          ? { ...sectorPoint, __kind: day.point.__kind, __quality: day.point.__quality, __methodology: day.point.__methodology }
          : null
      };
    });
  }

  function sectorHistoryValue(point, key) {
    if (!point) return null;
    const raw = key === "heat"
      ? point.heat ?? point.signals?.heat
      : key === "relativeHeat"
        ? point.relativeHeat ?? point.heatModel?.relativeHeat
        : key === "absoluteHeat"
          ? point.absoluteHeat ?? point.legacyHeat ?? point.signals?.heat
      : key === "direction"
        ? point.direction ?? point.signals?.optimism
        : point.scores?.[key] ?? point[key];
    const value = finiteNumber(raw);
    return value === null ? null : Math.max(0, Math.min(100, value));
  }

  function renderSectorMiniHistory(snapshot, sector) {
    const shell = $("#sector-history");
    const svg = $("#sector-history-chart");
    const legend = $("#sector-history-legend");
    const note = $("#sector-history-note");
    if (!shell || !svg || !legend || !note) return;

    const days = sectorHistoryDays(snapshot.history || [], snapshot, sector.key);
    const views = {
      personas: [
        ["baoma", "宝妈", palette.baoma],
        ["novice", "小白", palette.novice],
        ["retail", "散户", palette.retail]
      ],
      signals: [
        ["heat", "综合热度", "#bddc6d"],
        ["relativeHeat", "相对热度", "#efa946"],
        ["direction", "情绪方向", "#65b8d5"]
      ]
    };
    const width = 900;
    const height = 250;
    const left = 38;
    const right = 16;
    const top = 20;
    const bottom = 39;
    const plotW = width - left - right;
    const plotH = height - top - bottom;
    const x = (index) => left + (days.length <= 1 ? plotW / 2 : index * plotW / (days.length - 1));
    const y = (score) => top + plotH - Number(score) / 100 * plotH;
    const grid = [0, 50, 100].map((tick) =>
      `<line class="sector-history-grid${tick === 50 ? " is-neutral" : ""}" x1="${left}" x2="${width - right}" y1="${y(tick)}" y2="${y(tick)}"></line>
       <text class="sector-history-label" x="${left - 9}" y="${y(tick) + 4}" text-anchor="end">${tick}</text>`
    ).join("");

    function draw(viewName) {
      const series = views[viewName] || views.personas;
      const recorded = days.filter((day) => series.some(([key]) => sectorHistoryValue(day.point, key) !== null));
      const measured = recorded.filter((day) => day.point?.__kind === "measured").length;
      const estimated = recorded.filter((day) => day.point?.__kind === "estimated").length;
      const provisional = recorded.filter((day) => day.point?.__quality === "provisional").length;
      const usable = Math.max(0, estimated - provisional);
      const missing = Math.max(0, days.length - recorded.length);
      legend.innerHTML = series.map(([, name, color]) =>
        `<span><i style="background:${color}"></i>${escapeHtml(name)}</span>`
      ).join("");

      if (recorded.length <= 1) {
        const emptyText = recorded.length === 1 ? "历史积累中" : "暂无该板块历史记录";
        const loneProvisional = recorded[0]?.point?.__quality === "provisional";
        svg.innerHTML = `${grid}<text class="sector-history-empty" x="${width / 2}" y="${height / 2 - 4}">${emptyText}</text>
          <text class="sector-history-empty-copy" x="${width / 2}" y="${height / 2 + 18}">${recorded.length === 1 ? loneProvisional ? "当前为低置信回溯，待更多样本完善" : "交易日更新后会逐步形成趋势" : "完成历史构建或交易日更新后显示"}</text>`;
        note.textContent = recorded.length === 1
          ? `已有 1 个有效历史点 · ${loneProvisional ? "低置信回溯 / 待完善" : "历史积累中"}`
          : "最近 60 个交易日暂无有效板块历史点";
        svg.setAttribute("aria-label", `${sector.name}板块最近60个交易日${viewName === "signals" ? "热度与方向" : "三项指数"}趋势，${emptyText}${loneProvisional ? "，该点为低置信回溯待完善" : ""}`);
        return;
      }

      const lines = series.map(([key, , color]) => {
        const segments = [];
        for (let index = 1; index < days.length; index += 1) {
          const previous = days[index - 1].point;
          const current = days[index].point;
          const previousValue = sectorHistoryValue(previous, key);
          const currentValue = sectorHistoryValue(current, key);
          if (previousValue === null || currentValue === null) continue;
          if (previous.__methodology !== current.__methodology) continue;
          const provisionalSegment = previous.__quality === "provisional" || current.__quality === "provisional";
          const kind = provisionalSegment
            ? "is-provisional"
            : previous.__kind === "estimated" || current.__kind === "estimated" ? "is-estimated" : "is-measured";
          segments.push(`<line class="sector-history-line ${kind}" x1="${x(index - 1)}" y1="${y(previousValue)}" x2="${x(index)}" y2="${y(currentValue)}" stroke="${color}"></line>`);
        }
        return segments.join("");
      }).join("");
      const points = series.map(([key, name, color]) => days.map((day, index) => {
        const value = sectorHistoryValue(day.point, key);
        if (value === null) return "";
        const estimatedPoint = day.point.__kind === "estimated";
        return `<circle class="sector-history-point is-${historyVisualClass(day.point)}" cx="${x(index)}" cy="${y(value)}" r="${estimatedPoint ? 2.2 : 2.7}" fill="${estimatedPoint ? "#17352d" : color}" stroke="${color}"><title>${day.date} · ${historyStatusLabel(day.point)} · ${name} ${value.toFixed(1)}</title></circle>`;
      }).join("")).join("");
      const labelIndexes = [...new Set([0, Math.round((days.length - 1) / 2), days.length - 1])];
      const labels = labelIndexes.map((index) =>
        `<text class="sector-history-label" x="${x(index)}" y="${height - 12}" text-anchor="middle">${escapeHtml(days[index].date.slice(5))}</text>`
      ).join("");
      svg.innerHTML = `${grid}${lines}${points}${labels}`;
      note.textContent = `最近 60 个交易日 · 实测 ${measured} 个 · 可用回溯 ${usable} 个 · 低置信回溯待完善 ${provisional} 个${missing ? ` · 缺少板块值 ${missing} 个` : ""}`;
      svg.setAttribute("aria-label", `${sector.name}板块最近60个交易日${viewName === "signals" ? "社区热度与情绪方向" : "宝妈、小白、散户三项指数"}趋势，实测${measured}个，可用回溯${usable}个，低置信回溯待完善${provisional}个${missing ? `，缺少板块值${missing}个` : ""}；不补周末或休市空点`);
    }

    shell.querySelectorAll("[data-sector-history-view]").forEach((button) => {
      button.addEventListener("click", () => {
        shell.querySelectorAll("[data-sector-history-view]").forEach((item) => {
          const active = item === button;
          item.classList.toggle("active", active);
          item.setAttribute("aria-pressed", String(active));
        });
        draw(button.dataset.sectorHistoryView === "signals" ? "signals" : "personas");
      });
    });
    draw("signals");
  }

  function renderSectorDetail(snapshot, sector) {
    const content = $("#sector-detail-content");
    if (!content || !sector) return;

    const available = sectorIsAvailable(sector);
    const scores = sector.scores || {};
    const signals = sector.signals || {};
    const metrics = sector.metrics || {};
    const fairness = metrics.fairness || {};
    const rankEligible = fairness.rankEligible === true;
    const postStats = tradingDayPostStats(metrics);
    const sampleTotal = !available || postStats.total === null ? "--" : Math.round(postStats.total);
    const composite = available ? Number(scores.composite ?? 0) : null;
    const heat = available ? Number(sector.heat ?? signals.heat ?? 0) : null;
    const relativeHeat = available ? Number(sector.relativeHeat ?? sector.heatModel?.relativeHeat ?? 50) : null;
    const absoluteHeat = available ? Number(sector.absoluteHeat ?? sector.legacyHeat ?? signals.heat ?? 0) : null;
    const emotionHeat = available ? Number(sector.legacyHeat ?? signals.heat ?? 0) : null;
    const activityIndex = available ? Number(metrics.activityIndex ?? sector.heatModel?.activityIndex ?? signals.attention ?? 50) : null;
    const direction = available ? Number(sector.direction ?? signals.optimism ?? 0) : null;
    const directionText = available ? signalReading("optimism", direction) : "暂无方向判断";
    const personaKeys = ["baoma", "novice", "retail"];
    const signalKeys = ["optimism", "fomo", "panic", "newcomer", "intensity", "attention"];
    const platforms = Array.isArray(sector.platforms) ? sector.platforms : [];
    const forumSources = Array.isArray(sector.forumSources) ? sector.forumSources : [];
    const samplePosts = available && Array.isArray(sector.samplePosts) ? sector.samplePosts.slice(0, 6) : [];
    const parent = sectorParentInfo(sector);
    const representativeCompanies = sectorRepresentativeCompanies(sector);
    const rawDescription = String(sector.description ?? "").trim();
    const inclusionCopy = rawDescription || (representativeCompanies.length
      ? `这个细分板块以${representativeCompanies.map((company) => company.name || company.code).join("、")}等代表公司所在的相关产业方向为观察对象。`
      : "这里展示的是该板块公开社区的讨论温度与方向；当前数据暂未提供更细的成分范围说明。");
    const representativeCompanyCards = representativeCompanies.length ? `<div class="sector-detail-companies" aria-label="三家典型代表公司">
      <span>典型代表公司</span>
      <div>${representativeCompanies.map((company) => `<article title="${escapeHtml([company.name, company.code, company.role].filter(Boolean).join(" · "))}">
        <strong>${escapeHtml(company.name || company.code)}</strong>
        ${company.code ? `<code>${escapeHtml(company.code)}</code>` : `<code>代码未提供</code>`}
        ${company.role ? `<small>${escapeHtml(company.role)}</small>` : ""}
      </article>`).join("")}</div>
    </div>` : "";
    const unavailableDetailNotice = available ? "" : `<div class="sector-detail-missing" role="status">
      <strong>待首次成功更新</strong><span>当前缓存没有这个板块的可评分文本，因此本页不显示任何推测分数。下一次联网成功更新会自动补齐。</span>
    </div>`;
    const sectorEvidence = available ? personaEvidenceDisclosure(
      metrics.personaEvidence,
      "sector-persona-evidence-title",
      sector.name || "当前"
    ) : "";
    const companyWeights = Array.isArray(fairness.companyWeights) ? fairness.companyWeights : [];
    const dayWeights = Array.isArray(fairness.effectiveDayWeights) ? fairness.effectiveDayWeights : [];
    const platformWeights = Array.isArray(fairness.platformSummary) ? fairness.platformSummary : [];
    const stability = fairness.stabilityBand || {};
    const channelCoverage = Number(fairness.weightedChannelCoverage ?? fairness.weightedCompanyCoverage ?? 0);
    const channelNames = {
      eastmoney: "东方财富",
      sina: "新浪股吧",
      ths: "同花顺板块社区",
      taoguba: "淘股吧"
    };
    const weightPercent = (value) => {
      const number = Number(value || 0);
      return number > 1 ? number : number * 100;
    };
    const flagLabels = {
      low_reliability: "有效证据偏少",
      low_effective_sample: "有效样本偏少",
      unobserved_day: "存在未观测交易日",
      verified_zero_day: "存在经核验的零帖日",
      tiny_platform_downweighted: "极小平台样本已降权",
      ths_emotion_capped: "同花顺情绪权重已封顶",
      ths_excluded_from_volume_attention: "同花顺未计入声量与关注",
      ths_attention_disabled: "同花顺热门流不计正式发帖量与关注",
      missing_channels: "存在渠道席位缺失",
      missing_companies: "存在公司席位缺失",
      conditional_company_concentration_high: "条件证据集中，但正式席位未转让"
    };
    const fairnessPanel = available && fairness.modelVersion ? `<section class="sector-detail-section sector-fairness" aria-labelledby="sector-fairness-title">
      <div class="sector-detail-section-head"><div><span>FAIRNESS AUDIT</span><h3 id="sector-fairness-title">四渠道公平性与可信度</h3></div><p>东方财富、新浪、同花顺板块社区、淘股吧固定各占 25%；缺失席位保持中性、不转权，样本多只提高可靠度。</p></div>
      <div class="fairness-summary-grid">
        <article><span>证据可靠度</span><strong>${Number(fairness.reliabilityPercent || 0).toFixed(1)}%</strong><small>${escapeHtml(fairness.confidenceLabel || "低")}置信；不是预测胜率</small></article>
        <article><span>渠道加权覆盖</span><strong>${weightPercent(channelCoverage).toFixed(1)}%</strong><small>三个交易日中真实观测到的固定渠道席位</small></article>
        <article><span>留一渠道 / 来源扰动</span><strong>${Number(stability.low ?? composite ?? 50).toFixed(1)}–${Number(stability.high ?? composite ?? 50).toFixed(1)}</strong><small>逐个拿掉一个渠道或来源后的综合分范围，不是统计置信区间</small></article>
        <article class="${rankEligible ? "is-pass" : "is-hold"}"><span>排名资格</span><strong>${rankEligible ? "通过" : "暂缓"}</strong><small>${rankEligible ? "覆盖、样本与稳定性达到硬排名门槛" : "仍显示试算值，但不制造精确名次"}</small></article>
      </div>
      <div class="fairness-block"><h4>四个渠道固定席位</h4><div class="fairness-platform-list">${platformWeights.map((platform) => {
        const key = String(platform.key || platform.platform || "");
        const target = weightPercent(platform.targetWeight ?? 0.25);
        const influence = weightPercent(platform.formalInfluence ?? platform.emotionWeight ?? 0);
        const neutral = weightPercent(platform.neutralisedWeight ?? Math.max(0, target - influence) / 100);
        const expected = Number(platform.expectedUnitCount ?? 0);
        const observed = Number(platform.observedUnitCount ?? 0);
        const reliability = Number(platform.reliabilityPercent ?? fairness.reliabilityPercent ?? 0);
        const allowAttention = platform.allowAttention ?? platform.volumeEligible ?? true;
        return `<article>
          <div><b>${escapeHtml(platform.name || channelNames[key] || key || "渠道")}</b><em>目标 ${target.toFixed(0)}%</em></div>
          <p><span>正式影响 ${influence.toFixed(1)}% · <span class="${neutral > 0 ? "is-neutral" : ""}">中性保留 ${neutral.toFixed(1)}%</span></span><span>入口覆盖 ${observed}/${expected || "--"} · 可靠度 ${reliability.toFixed(0)}%</span><span>${allowAttention === false ? "热门流只计文本情绪，不计正式发帖量与关注" : "参与文本情绪及合格的时序活跃信号"}</span></p>
        </article>`;
      }).join("") || "<p class=\"sector-detail-empty\">暂无渠道权重诊断。</p>"}</div></div>
      ${companyWeights.length ? `<div class="fairness-block"><h4>渠道内公司席位</h4><div class="fairness-company-grid">${companyWeights.map((company) => `<article>
        <div><strong>${escapeHtml(company.name || company.key || "公司席位")}</strong><code>${escapeHtml(company.key === "__missing__" ? "缺失" : company.key || "")}</code></div>
        <p><span>目标权重 ${(Number(company.targetWeight || 0) * 100).toFixed(1)}%</span><span>正式有效 ${(Number(company.effectiveWeight || 0) * 100).toFixed(1)}%</span><span>中性保留 ${(Number(company.neutralisedWeight || 0) * 100).toFixed(1)}%</span></p>
      </article>`).join("")}</div></div>` : ""}
      <div class="fairness-block"><h4>三个交易日固定权重</h4><div class="fairness-day-grid">${dayWeights.map((day, index) => `<article>
        <span>${index === 0 ? "最近交易日" : index === 1 ? "上一交易日" : "上两个交易日"}</span><strong>${weightPercent(day.weight || 0).toFixed(0)}%</strong><small>${escapeHtml(day.date || "--")} · 观测 ${Number(day.observedChannelCount ?? day.observedCompanyCount ?? 0)}/${Number(day.expectedChannelCount ?? 4)} 个渠道 · ${day.status === "unobserved" ? "未观测，不重分权重" : day.status === "verified_zero" ? "已核验零帖" : "有真实文本"}</small>
      </article>`).join("")}</div></div>
      ${Array.isArray(fairness.flags) && fairness.flags.length ? `<p class="fairness-flags"><strong>自动提示：</strong>${fairness.flags.map((flag) => escapeHtml(flagLabels[flag] || flag)).join("；")}</p>` : ""}
    </section>` : "";

    const personaCards = personaKeys.map((key) => {
      const meta = indexMeta[key];
      const score = available ? Number(scores[key] ?? 0) : null;
      const scoreText = available ? score.toFixed(0) : "--";
      return `<article class="sector-detail-persona-card is-${key}${available ? "" : " is-unavailable"}">
        <div class="sector-detail-card-top">
          <div><span>${available ? meta.kicker : "待更新"}</span><h4>${meta.name}</h4></div>
          <strong>${scoreText}</strong>
        </div>
        <div class="sector-detail-meter" aria-label="${escapeHtml(meta.name)} ${available ? `${scoreText} 分` : "暂无评分"}"><i style="width:${available ? score : 0}%;background:${palette[key]}"></i></div>
        <p>${meta.copy}</p>
      </article>`;
    }).join("");

    const signalCards = signalKeys.map((key) => {
      const meta = signalMeta[key];
      const score = available ? Number(signals[key] ?? 0) : null;
      const scoreText = available ? score.toFixed(1) : "--";
      return `<article class="sector-detail-signal-card${available ? "" : " is-unavailable"}">
        <div class="sector-detail-card-top">
          <div><span>${available ? escapeHtml(signalReading(key, score)) : "待更新"}</span><h4>${escapeHtml(meta[0])}</h4></div>
          <strong${available ? ` style="color:${colorFor(score)}"` : ""}>${scoreText}</strong>
        </div>
        <p>${escapeHtml(meta[1])}</p>
        <div class="sector-detail-meter" aria-label="${escapeHtml(meta[0])} ${available ? `${scoreText} 分` : "暂无评分"}"><i style="width:${available ? score : 0}%;${available ? `background:${colorFor(score)}` : ""}"></i></div>
      </article>`;
    }).join("");

    const statItems = [
      ...postStats.daily.map((item) => [`${item.label}帖子`, available ? tradingDayCountText(item) : "--", available ? "过滤、去重并执行作者限额后的有效公开讨论" : "等待下一次联网成功更新"]),
      ["三交易日有效样本", sampleTotal === "--" ? "--" : `${sampleTotal} 条`, "最近三个 A 股交易日用于当前观察的有效社区文本"],
      ["加权日帖子量", available && postStats.weighted !== null ? `${postStats.weighted.toFixed(1)} 条` : "--", "三个交易日由近到远按 50% / 30% / 20% 加权"],
      ["关注活跃位置", available ? activityIndex.toFixed(1) : "--", "有效帖子数量、发帖速度和参与者在各自渠道内校准后的位置；同花顺热门流不计入此项"],
      ["三交易日参与者", available ? `${Number(metrics.uniqueAuthors3d ?? metrics.uniqueAuthors ?? metrics.uniqueAuthors24 ?? 0)} 位` : "--", "作者限额与去重后的最近三个交易日公开参与者"],
      ["看多 / 看空", available ? `${Number(metrics.bullishRatio || 0).toFixed(1)}% / ${Number(metrics.bearishRatio || 0).toFixed(1)}%` : "--", "样本中明确表达方向的占比"],
      ["覆盖平台", available ? `${Number(metrics.platformCount || platforms.length || 0)} 个` : "--", "本次成功读到的公开社区平台"],
      ["社区来源", available ? `${Number(metrics.sourceCount || forumSources.length || 0)} 个` : "--", "本板块对应的论坛或个股讨论区"]
    ];
    const stats = statItems.map(([label, value, copy]) => `<div class="sector-detail-stat"><span>${label}</span><strong>${value}</strong><small>${copy}</small></div>`).join("");

    const platformCards = available && platforms.length ? platforms.map((platform) => {
      const platformSignals = platform.signals || {};
      return `<article class="sector-platform-card">
        <div><h4>${escapeHtml(platform.name || platform.key || "公开社区")}</h4><span>${Number(platform.sampleCount || 0)} 条样本</span></div>
        <dl>
          <div><dt>本次有效样本</dt><dd>${Number(platform.sampleCount || 0)} 条</dd></div>
          <div><dt>正式情绪影响</dt><dd>${Number(platform.weight || 0).toFixed(1)}%</dd></div>
          <div><dt>正式关注影响</dt><dd>${Number(platform.attentionWeight || 0).toFixed(1)}%</dd></div>
          <div><dt>平台方向诊断</dt><dd>${Number(platformSignals.optimism || 0).toFixed(1)}</dd></div>
        </dl>
      </article>`;
    }).join("") : `<p class="sector-detail-empty">${available ? "本次没有可展示的平台拆分信息。" : "当前缓存没有可评分文本，平台分数将在下一次联网成功更新后补齐。"}</p>`;

    const sourceLinks = forumSources.length ? forumSources.map((source) => {
      const href = safeHttpUrl(source.url);
      const sourceStatus = available ? (source.fresh === false ? " · 缓存" : " · 本次已读取") : " · 待联网读取";
      const coverage = source.coverage || {};
      const coverageText = coverage.chronological
        ? coverage.thirdTradingDayReached ? " · 已到第三交易日" : coverage.capHit ? " · 分页上限前未完整" : " · 时序覆盖待确认"
        : " · 非时序热榜";
      const inner = `<span>${escapeHtml(source.name || source.code || "公开讨论区")}</span><small>${escapeHtml(source.platformName || source.platform || "社区来源")}${sourceStatus}${coverageText}</small>`;
      return href
        ? `<a class="sector-source-link" href="${escapeHtml(href)}" target="_blank" rel="noopener noreferrer">${inner}<b aria-hidden="true">↗</b></a>`
        : `<div class="sector-source-link is-disabled">${inner}</div>`;
    }).join("") : `<p class="sector-detail-empty">本次没有可展示的来源链接。</p>`;

    const postCards = samplePosts.length ? samplePosts.map((post) => {
      const comments = Number(post.comments || 0);
      const clicks = Number(post.clicks || 0);
      return `<article class="sector-post-card">
        <div class="sector-post-top"><span class="${sentimentClass(post.label)}">${escapeHtml(post.label || "中性")}</span><span>${escapeHtml(post.platformName || post.source || "公开社区")}</span></div>
        <p>${escapeHtml(post.title || "无标题讨论")}</p>
        <div class="sector-post-meta"><span>${escapeHtml(post.source || "社区样本")}</span><span>${formatPostTime(post.publishTime)} · ${comments} 评论${clicks ? ` · ${clicks} 公开互动/浏览` : ""}</span></div>
      </article>`;
    }).join("") : `<p class="sector-detail-empty">${available ? "本次没有可展示的代表讨论。" : "当前缓存没有该板块的可评分讨论，下一次联网成功更新后会在这里显示。"}</p>`;

    content.innerHTML = `
      <header class="sector-detail-header">
        <div>
          <p class="eyebrow">SECTOR COMMUNITY DETAIL</p>
          ${parent ? `<div class="sector-detail-breadcrumb"><span>${escapeHtml(parent.name)}</span><b aria-hidden="true">→</b><strong>${escapeHtml(sector.name)}</strong></div>` : ""}
          <h2 id="sector-dialog-title">${escapeHtml(sector.name)}板块</h2>
          <p id="sector-dialog-summary">${escapeHtml(inclusionCopy)}</p>
          ${representativeCompanyCards}
          ${unavailableDetailNotice}
          <p class="sector-detail-disclaimer">这里展示的是公开社区的讨论温度与方向，不代表股价涨跌或投资建议。</p>
        </div>
        <span class="sector-detail-date">最近交易日 ${escapeHtml(snapshot.marketDate || "--")}<br>${escapeHtml(formatDateTime(snapshot.updatedAt))} 查看</span>
      </header>

      <section class="sector-detail-overview${available ? "" : " is-unavailable"}" aria-label="板块概览">
        <div class="sector-detail-score"${available ? ` style="--detail-color:${colorFor(heat)}"` : ""}>
          <span>新版综合热度</span><strong>${available ? heat.toFixed(1) : "--"}</strong><em>${available ? escapeHtml(sector.heatTier || levelFor(heat)) : "待首次成功更新"}</em>
        </div>
        <div class="sector-detail-reading">
          <span class="sector-detail-kicker">${available ? "当前怎么读" : "数据状态"}</span>
          <h3>${available ? `${escapeHtml(directionText)}，${escapeHtml(sector.heatTier || (heat >= 65 ? "板块偏热" : heat >= 50 ? "讨论活跃" : "板块偏冷"))}` : "当前缓存没有可评分文本"}</h3>
          <p>${available ? "板块热度 V6 先在每个渠道当日校准，再让东方财富、新浪、同花顺、淘股吧固定各占 25%，最后由近到远按 50% / 30% / 20% 合成；缺失席位中性保留，情绪方向仍单独表示偏多或偏空。" : "为了避免把缺失数据误解为中性，本页所有指数、热度和信号暂以“--”显示；下一次联网成功更新后会自动恢复评分。"}</p>
          <div class="sector-detail-chips">
            <span>三指数综合 ${helpBadge("composite")} <strong>${available ? composite.toFixed(0) : "--"}</strong></span>
            <span>热度排名 <strong>${available && sector.heatRank ? `#${Number(sector.heatRank)}` : available ? "暂不硬排名" : "--"}</strong></span>
            <span>相对热度 ${helpBadge("relativeHeat")} <strong>${available ? relativeHeat.toFixed(1) : "--"}</strong></span>
            <span>绝对温度 ${helpBadge("absoluteHeat")} <strong>${available ? absoluteHeat.toFixed(1) : "--"}</strong></span>
            <span>情绪躁动 ${helpBadge("legacyHeat")} <strong>${available ? emotionHeat.toFixed(1) : "--"}</strong></span>
            <span>情绪方向 <strong>${available ? direction.toFixed(1) : "--"}</strong></span>
            <span>三交易日样本 <strong>${sampleTotal === "--" ? "--" : `${sampleTotal} 条`}</strong></span>
          </div>
        </div>
      </section>

      ${fairnessPanel}

      <section class="sector-detail-section" aria-labelledby="sector-persona-title">
        <div class="sector-detail-section-head"><div><span>THREE VIEWS</span><h3 id="sector-persona-title">宝妈、小白、散户三大指数</h3></div><p>${available ? "同一批板块讨论的三种表达视角，不识别发帖人的真实身份、性别、年龄或账户类型。" : "当前没有可评分文本，三项指数暂不估算。"}</p></div>
        <div class="sector-detail-personas">${personaCards}</div>
      </section>

      ${sectorEvidence ? `<section class="sector-detail-section persona-evidence-panel is-sector" aria-labelledby="sector-persona-evidence-title">${sectorEvidence}</section>` : ""}

      <section class="sector-detail-section sector-history" id="sector-history" aria-labelledby="sector-history-title">
        <div class="sector-detail-section-head sector-history-head">
          <div><span>60 TRADING-DAY TREND</span><h3 id="sector-history-title">板块最近 60 个交易日趋势</h3></div>
          <div class="sector-history-toggle" role="group" aria-label="选择板块历史指标">
            <button type="button" data-sector-history-view="personas" aria-pressed="false">三项指数</button>
            <button type="button" class="active" data-sector-history-view="signals" aria-pressed="true">新版热度/相对/方向</button>
          </div>
        </div>
          <p class="history-method-break">口径提示：四渠道固定席位 V6 从当前更新日开始。此前历史保留用于查看旧口径内部变化，但方法断点两侧的绝对分数不直接横比。</p>
        <div class="sector-history-card">
          <div class="sector-history-meta">
            <div class="sector-history-legend" id="sector-history-legend" aria-label="趋势图指标图例"></div>
            <div class="sector-history-kind" aria-label="数据类型图例"><span><i></i>交易日实测</span><span><i class="is-estimated"></i>可用回溯</span><span><i class="is-provisional"></i>低置信回溯 / 待完善</span><span><i class="is-gap"></i>缺少板块值</span></div>
          </div>
          <svg id="sector-history-chart" class="sector-history-chart" viewBox="0 0 900 250" role="img" tabindex="0" aria-label="板块最近60个交易日趋势"></svg>
          <p class="sector-history-note" id="sector-history-note" role="status" aria-live="polite"></p>
        </div>
      </section>

      <section class="sector-detail-section" aria-labelledby="sector-signal-title">
        <div class="sector-detail-section-head"><div><span>SIX SIGNALS</span><h3 id="sector-signal-title">六项社区情绪与方向信号</h3></div><p>${available ? "六项信号保留原始含义；板块综合热度在上方单独展示。" : "当前没有可评分文本，六项信号暂不估算。"}</p></div>
        <div class="sector-detail-signals">${signalCards}</div>
      </section>

      <section class="sector-detail-section" aria-labelledby="sector-sample-title">
        <div class="sector-detail-section-head"><div><span>THREE TRADING DAYS</span><h3 id="sector-sample-title">三交易日样本概况</h3></div><p>${available ? "逐日查看帖子量，并核对 50% / 30% / 20% 加权后的活跃依据。" : "待联网采集成功后再生成样本统计。"}</p></div>
        <div class="sector-detail-stats">${stats}</div>
      </section>

      <section class="sector-detail-section" aria-labelledby="sector-platform-title">
        <div class="sector-detail-section-head"><div><span>PLATFORMS</span><h3 id="sector-platform-title">平台观察</h3></div><p>平台卡片用于核验样本；正式影响以公平性面板为准，不按原始帖子数直接加权。</p></div>
        <div class="sector-platform-grid">${platformCards}</div>
      </section>

      <section class="sector-detail-section" aria-labelledby="sector-source-title">
        <div class="sector-detail-section-head"><div><span>PUBLIC SOURCES</span><h3 id="sector-source-title">对应社区来源</h3></div><p>可以打开公开页面自行核验。</p></div>
        <div class="sector-source-grid">${sourceLinks}</div>
      </section>

      <section class="sector-detail-section" aria-labelledby="sector-post-title">
        <div class="sector-detail-section-head"><div><span>REPRESENTATIVE POSTS</span><h3 id="sector-post-title">代表讨论</h3></div><p>展示本板块本次样本中的短文本摘录。</p></div>
        <div class="sector-post-grid">${postCards}</div>
      </section>`;
    renderSectorMiniHistory(snapshot, sector);
  }

  function installSectorDetails(snapshot) {
    const grid = $("#sector-grid");
    const map = $("#sector-map");
    const dialog = $("#sector-dialog");
    const closeButton = dialog?.querySelector("[data-sector-close]");
    if (!grid || !dialog || !closeButton) return;

    const sectorMap = new Map((snapshot.sectorIndices || []).map((sector) => [String(sector.key || sector.name), sector]));
    let lastTrigger = null;

    function closeDialog() {
      if (typeof dialog.close === "function" && dialog.open) dialog.close();
      else {
        dialog.removeAttribute("open");
        document.body.classList.remove("sector-dialog-open");
        lastTrigger?.focus?.();
      }
    }

    function openDialog(key, trigger) {
      const sector = sectorMap.get(String(key || ""));
      if (!sector) return;
      lastTrigger = trigger || null;
      lastTrigger?.focus?.({ preventScroll: true });
      renderSectorDetail(snapshot, sector);
      const shell = dialog.querySelector(".sector-dialog-shell");
      if (shell) shell.scrollTop = 0;
      if (typeof dialog.showModal === "function") dialog.showModal();
      else dialog.setAttribute("open", "");
      document.body.classList.add("sector-dialog-open");
      closeButton.focus();
    }

    grid.addEventListener("click", (event) => {
      if (event.target.closest?.(".metric-help")) return;
      const card = event.target.closest?.(".sector-card[data-sector-key]");
      if (!card) return;
      const button = event.target.closest?.("[data-sector-open]");
      const focusTarget = button || card.querySelector("[data-sector-open]") || card;
      openDialog(button?.dataset.sectorOpen || card.dataset.sectorKey, focusTarget);
    });
    (map?.closest(".sector-map-layout") || map)?.addEventListener("click", (event) => {
      const button = event.target.closest?.("[data-sector-map-open]");
      if (!button) return;
      openDialog(button.dataset.sectorMapOpen, button);
    });
    closeButton.addEventListener("click", closeDialog);
    dialog.addEventListener("click", (event) => {
      if (event.target === dialog) closeDialog();
    });
    dialog.addEventListener("close", () => {
      document.body.classList.remove("sector-dialog-open");
      lastTrigger?.focus?.();
    });
  }

  function uniqueDisplayPosts(posts, limit = 20) {
    const seen = new Set();
    const selected = [];
    const ordered = [...posts].sort((a, b) => String(b.publishTime || "").localeCompare(String(a.publishTime || "")));
    for (const post of ordered) {
      const key = String(post.title || "").toLowerCase().replace(/[^0-9a-z\u4e00-\u9fff]/g, "");
      if (!key || seen.has(key)) continue;
      seen.add(key);
      selected.push(post);
      if (selected.length >= limit) break;
    }
    return selected;
  }

  function renderRanks(snapshot) {
    const sectors = [...(snapshot.sectorIndices || [])]
      .filter((item) => sectorIsAvailable(item) && item.metrics?.fairness?.rankEligible === true && item.heatRank)
      .sort((a, b) => Number(a.heatRank || 999) - Number(b.heatRank || 999))
      .slice(0, 9);
    $("#board-list").innerHTML = sectors.length ? sectors.map((item, index) =>
      `<div class="rank-item">
        <span class="rank-no">${String(index + 1).padStart(2, "0")}</span>
        <div class="rank-main"><strong>${escapeHtml(item.name)}</strong><span>相对 ${Number(item.relativeHeat ?? 50).toFixed(1)} · 宝妈 ${Number(item.scores?.baoma || 0)} · 小白 ${Number(item.scores?.novice || 0)} · 散户 ${Number(item.scores?.retail || 0)}</span></div>
        <div class="rank-value" title="${escapeHtml(metricHelp.heat)}"><strong style="color:${colorFor(Number(item.heat || 0))}">${Number(item.heat || 0).toFixed(1)}</strong><span>${escapeHtml(item.heatTier || levelFor(Number(item.heat || 0)))} ${helpBadge("heat")}</span></div>
      </div>`
    ).join("") : `<div class="rank-item"><span class="rank-no">--</span><div class="rank-main"><strong>暂不生成硬排名</strong><span>待至少 5 个板块通过渠道覆盖、有效样本与稳定性门槛后自动恢复</span></div></div>`;

    const posts = uniqueDisplayPosts(snapshot.community?.samplePosts || []);
    $("#hot-source").textContent = snapshot.sources?.community || "普通用户公开标题";
    $("#stock-list").innerHTML = posts.length ? posts.map((item, index) =>
      `<div class="rank-item">
        <span class="rank-no">${String(index + 1).padStart(2, "0")}</span>
        <div class="rank-main"><strong>${escapeHtml(item.title)}</strong><span>${escapeHtml(item.source)} · ${formatPostTime(item.publishTime)}${item.authorKey ? ` · 用户标识 ${escapeHtml(item.authorKey)}` : ""}${item.postId ? ` · 帖子 ${escapeHtml(item.postId)}` : ""}</span></div>
        <div class="rank-value"><strong class="${sentimentClass(item.label)}">${escapeHtml(item.label)}</strong><span>${Number(item.comments || 0)} 评论（展示，不计分）</span></div>
      </div>`
    ).join("") : `<div class="rank-item"><span class="rank-no">--</span><div class="rank-main"><strong>社区样本暂不可用</strong><span>程序会在下次更新时重试</span></div></div>`;
  }

  function renderMarketEvidence(snapshot) {
    const panel = $("#market-evidence");
    const coverage = snapshot.diagnostics?.marketWindowCoverage;
    if (!panel || !coverage?.sources?.length) return;
    panel.hidden = false;
    const names = { eastmoney: "东方财富", sina: "新浪", ths: "同花顺", taoguba: "淘股吧" };
    const reliability = Number(snapshot.diagnostics?.fairness?.reliabilityPercent || 0);
    $("#market-evidence-summary").textContent = "大盘证据可靠度 " + reliability.toFixed(1) + "%（不是预测准确率）";
    const missing = (coverage.missingChannels || []).map((key) => names[key] || key).join("、");
    const reused = coverage.sources.reduce((sum, row) => sum + Number(row.archiveReusedPosts || 0), 0);
    $("#market-evidence-note").textContent = "有样本不等于时序完整；档案补样 " + reused + " 条。单元格显示有效样本数及覆盖状态。" + (missing ? "缺失渠道：" + missing + "，其席位保留中性。" : "");
    const headings = (coverage.tradingDays || []).map((day) => '<th scope="col">' + escapeHtml(day) + '</th>').join("");
    const stops = { window_start_covered: "窗口边界已覆盖", source_exhausted_before_third_trading_day: "公开流已读完", time_budget_exhausted: "达到时间预算", page_cap_reached_before_third_trading_day: "达到页数预算", repeated_page: "遇到重复页", partial_fetch_failure: "中途读取失败", non_chronological_hot_feed: "热门推荐流", cursor_stalled_before_third_trading_day: "翻页游标停滞", archive_samples_only: "仅档案样本" };
    const rows = coverage.sources.map((row) => {
      const cells = (row.days || []).map((day) => {
        const status = day.verifiedZero ? "核验零帖" : day.status !== "observed" ? "未观测" : day.streamCovered ? "时序已覆盖" : "时序不完整";
        return '<td>' + Number(day.sampleCount || 0) + ' 条<br><small>' + status + '</small></td>';
      }).join("");
      const time = row.fetchedAt ? escapeHtml(formatDateTime(row.fetchedAt)) + (!row.freshFetchedNow ? " · 缓存" : "") : "仅档案样本";
      return '<tr><th scope="row">' + escapeHtml(names[row.platform] || row.platform) + ' · ' + escapeHtml(row.name) + '</th>' + cells + '<td>' + Number(row.pageCount || 0) + ' 页' + (row.pageLimit ? ' / 上限 ' + Number(row.pageLimit) : '') + '<br><small>' + escapeHtml(stops[row.coverageStatus] || '覆盖待核验') + '<br>' + time + '</small></td></tr>';
    }).join("");
    $("#market-evidence-table").innerHTML = '<table class="history-table"><caption>大盘固定入口的三个交易日证据；时序覆盖不代表社区全量普查</caption><thead><tr><th scope="col">讨论入口</th>' + headings + '<th scope="col">本轮读取</th></tr></thead><tbody>' + rows + '</tbody></table>';
  }

  function renderEmpty() {
    const heatSnapshot = $("#heat-snapshot");
    if (heatSnapshot) heatSnapshot.innerHTML = `<div class="heat-snapshot-loading">等待首次更新后生成新版板块热度。</div>`;
    $("#index-cards").innerHTML = Object.entries(indexMeta).map(([key, meta]) => `<article class="index-card">
      <div class="card-top"><div><span class="card-kicker">${meta.kicker}</span><h3>${meta.name} ${helpBadge(key)}</h3></div><span class="mini-level">未更新</span></div>
      <div class="card-score-row"><strong>--</strong><span>/ 100</span></div>
      <div class="card-meter"><i style="width:0"></i></div><p class="card-copy">${meta.copy}</p>
    </article>`).join("");
    renderChart([]);
    $("#signal-grid").innerHTML = Object.entries(signalMeta).map(([key, meta]) => `<article class="signal-card"><div><h3>${meta[0]} ${helpBadge(key)}</h3><strong>--</strong></div><p>${meta[1]}</p><div class="signal-meter"><i style="width:0"></i></div></article>`).join("");
    renderSectorMap({ sectorIndices: [] });
    $("#board-list").innerHTML = `<div class="rank-item"><span class="rank-no">01</span><div class="rank-main"><strong>等待首次社区更新</strong><span>双击“一键更新指数.cmd”</span></div></div>`;
    $("#stock-list").innerHTML = $("#board-list").innerHTML;
    $("#sector-grid").innerHTML = `<article class="sector-card"><div class="sector-title"><span>WAITING</span><h3>等待首次更新</h3></div><p class="sector-foot">板块社区矩阵会在更新后显示。</p></article>`;
  }

  function installSectionNavigation() {
    const main = $("#main-content");
    if (!main) return;
    const sections = [...main.children].filter((node) => node.tagName === "SECTION");
    const groups = [
      { id: "overview", label: "市场总览", icon: "◉", indexes: [0, 1, 3, 4] },
      { id: "indices", label: "三个分指数", icon: "▤" },
      { id: "history", label: "历史趋势", icon: "↗", indexes: [5] },
      { id: "signals", label: "社区信号", icon: "◌", indexes: [6] },
      { id: "map", label: "板块地图", icon: "◈", indexes: [7] },
      { id: "sectors", label: "板块排行", icon: "▥", indexes: [8] },
      { id: "posts", label: "原帖抽样", icon: "☷", indexes: [9] },
      { id: "method", label: "计算口径", icon: "⚙", indexes: [10, 11] },
      { id: "evidence", label: "证据覆盖", icon: "◎", indexes: [2] }
    ];
    // Identify sections by their actual content: the public build omits evidence.
    const matchers = {
      overview: (s) => s.matches(".hero, .scale-strip, #heat-snapshot"),
      indices: (s) => !!s.querySelector("#index-cards"),
      history: (s) => s.matches(".chart-section"),
      signals: (s) => !!s.querySelector("#signal-grid"),
      map: (s) => s.matches(".sector-map-section"),
      sectors: (s) => s.matches(".sector-section"),
      posts: (s) => s.matches(".split-section"),
      method: (s) => s.matches(".method-section, .note-card"),
      evidence: (s) => s.id === "market-evidence"
    };
    const available = groups.filter((group) => sections.some(matchers[group.id]));
    const nav = document.createElement("nav");
    nav.className = "section-navigation";
    nav.setAttribute("aria-label", "仪表盘内容导航");
    nav.innerHTML = '<div class="nav-caption">内容导航</div>' + available.map((g) => '<button type="button" data-section-view="' + g.id + '"><span class="nav-icon" aria-hidden="true">' + g.icon + '</span><span>' + g.label + '</span></button>').join("");
    main.before(nav);
    document.body.classList.add("has-section-navigation");
    function show(id, focus = false) {
      if (!available.some((g) => g.id === id)) id = "overview";
      for (const section of sections) {
        section.classList.toggle("view-inactive", !matchers[id](section));
      }
      for (const button of nav.querySelectorAll("button")) {
        const active = button.dataset.sectionView === id;
        button.classList.toggle("active", active);
        if (active) button.setAttribute("aria-current", "page");
        else button.removeAttribute("aria-current");
      }
      if (focus) { main.focus({ preventScroll: true }); window.scrollTo({ top: 0, behavior: "smooth" }); }
    }
    nav.addEventListener("click", (event) => {
      const button = event.target.closest("button[data-section-view]");
      if (button) show(button.dataset.sectionView, true);
    });
    show("overview");
  }

  installMetricHelp();
  installSectionNavigation();

  if (!data || !data.scores || Number(data.version || 0) < 4) {
    renderEmpty();
    installHistoryRangeControls([], data || {});
    return;
  }

  renderHeader(data);
  renderMarketEvidence(data);
  renderHeatSnapshot(data);
  renderIndexCards(data);
  renderPersonaEvidenceOverview(data);
  renderChart(data.history || []);
  installHistoryRangeControls(data.history || [], data);
  renderSignals(data);
  renderSectorMap(data);
  renderSectorMatrix(data, "heat");
  renderRanks(data);
  installSectorDetails(data);

  document.querySelectorAll("[data-sector-sort]").forEach((button) => {
    button.addEventListener("click", () => {
      document.querySelectorAll("[data-sector-sort]").forEach((item) => {
        item.classList.remove("active");
        item.setAttribute("aria-pressed", "false");
      });
      button.classList.add("active");
      button.setAttribute("aria-pressed", "true");
      renderSectorMatrix(data, button.dataset.sectorSort || "heat", sectorViewState.parentKey);
    });
  });

  $("#sector-parent-filter")?.addEventListener("click", (event) => {
    const button = event.target.closest?.("[data-sector-parent]");
    if (!button) return;
    renderSectorMatrix(data, sectorViewState.sortKey, button.dataset.sectorParent || "all");
  });
})();
