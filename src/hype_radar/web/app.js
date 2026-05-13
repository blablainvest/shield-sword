let currentReport = null;
let researchCards = [];
let searchResultCard = null;
let searchStatus = { state: "idle", message: "" };
let selectedView = "live";
const SEARCH_TIMEOUT_MS = 120_000;
const sortState = {
  topLong: { key: "price_change_pct", direction: "desc" },
  topShort: { key: "price_change_pct", direction: "asc" }
};

const stageOrder = [
  "market_scan",
  "initial_selection",
  "research_charts",
  "fundamentals",
  "social_filter",
  "manipulation_detector",
  "technical_analysis",
  "trade_plan",
  "final_ranking"
];

document.querySelectorAll(".nav-item").forEach((button) => {
  button.addEventListener("click", () => {
    activateView(button.dataset.view);
    render();
  });
});

document.getElementById("runScan").addEventListener("click", runScan);
document.getElementById("scanWindow").addEventListener("change", render);
document.getElementById("closeDrawer").addEventListener("click", () => document.getElementById("drawer").classList.remove("open"));
document.getElementById("searchForm").addEventListener("submit", runSearch);
document.getElementById("pipelineSearch").addEventListener("input", renderPipelineBoard);
document.getElementById("backtestSearch").addEventListener("input", renderBacktestTable);
document.getElementById("backtestSetupFilter").addEventListener("change", renderBacktestTable);

loadInitial();

async function loadInitial() {
  await Promise.all([loadLatest(), loadResearchCards()]);
}

async function loadLatest() {
  const response = await fetch("/api/scan/latest");
  if (response.ok) {
    currentReport = await response.json();
    syncScanWindowFromReport();
    render();
  }
}

async function loadResearchCards() {
  const response = await fetch("/api/research");
  if (!response.ok) return;
  const payload = await response.json();
  researchCards = sortResearchCards(payload.research || []);
  renderPipelineBoard();
  renderBacktestTable();
}

async function runScan() {
  const button = document.getElementById("runScan");
  button.disabled = true;
  button.textContent = "Сканируем...";
  const topPerSide = document.getElementById("maxSymbols").value || 5;
  const minVolume = minVolumeUsdt();
  const windowHours = scanWindowHours();
  try {
    const response = await fetch(`/api/scan/run?top=${topPerSide}&max_symbols=${Number(topPerSide) * 2}&min_volume=${minVolume}&window_hours=${windowHours}`, { method: "POST" });
    currentReport = await response.json();
    syncScanWindowFromReport();
    render();
  } finally {
    button.disabled = false;
    button.textContent = "Запустить скан";
  }
}

async function runResearch(symbol, button) {
  if (button) {
    button.disabled = true;
    button.textContent = "Исследуем...";
  }
  const minVolume = minVolumeUsdt();
  const windowHours = scanWindowHours();
  try {
    const response = await fetch(`/api/research/run?symbol=${encodeURIComponent(symbol)}&min_volume=${minVolume}&window_hours=${windowHours}`, { method: "POST" });
    const card = await response.json();
    researchCards = sortResearchCards([card, ...researchCards]);
    selectedView = "pipeline";
    activateView("pipeline");
    render();
    openResearchCard(card.symbol, card.run_id, card.research_id);
  } finally {
    if (button) {
      button.disabled = false;
      applyResearchActionState(button, symbol);
    }
  }
}

async function runSearch(event) {
  event.preventDefault();
  const input = document.getElementById("searchInput").value.trim();
  const button = document.getElementById("runSearch");
  if (!input) {
    searchStatus = { state: "error", message: "Введите тикер или ссылку Bybit." };
    renderSearchResult();
    return;
  }
  setSearchButtonLoading(true);
  searchStatus = { state: "loading", message: "Запускаем реальный research pipeline." };
  renderSearchResult();
  const minVolume = minVolumeUsdt();
  const windowHours = searchWindowHours();
  try {
    const response = await fetchWithTimeout(
      `/api/search/run?query=${encodeURIComponent(input)}&min_volume=${minVolume}&window_hours=${windowHours}`,
      { method: "POST" },
      SEARCH_TIMEOUT_MS
    );
    const payload = await response.json();
    if (!response.ok) {
      throw new Error(payload.error || "Не удалось запустить анализ.");
    }
    searchResultCard = payload;
    researchCards = sortResearchCards([payload, ...researchCards]);
    searchStatus = { state: "ready", message: "" };
    render();
  } catch (error) {
    searchStatus = { state: "error", message: error.message || "Не удалось запустить анализ." };
    renderSearchResult();
  } finally {
    setSearchButtonLoading(false);
  }
}

async function fetchWithTimeout(url, options = {}, timeoutMs = 60_000) {
  const controller = new AbortController();
  const timeoutId = window.setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await fetch(url, { ...options, signal: controller.signal });
  } catch (error) {
    if (error?.name === "AbortError") {
      throw new Error("Research pipeline не ответил за 2 минуты. Запрос остановлен; попробуйте меньший период или повторите.");
    }
    throw error;
  } finally {
    window.clearTimeout(timeoutId);
  }
}

function setSearchButtonLoading(isLoading) {
  const currentButton = document.getElementById("runSearch");
  if (!currentButton) return;
  currentButton.disabled = Boolean(isLoading);
  currentButton.textContent = isLoading ? "Ищем..." : "Найти";
}

function render() {
  updatePageChrome();
  if (currentReport) {
    const hours = reportWindowHours();
    const run = currentReport.run || {};
    const summary = run.summary || {};
    const topRows = limitedMoverRows(currentReport.top_gainers_pipeline || currentReport.top_gainers_24h_pipeline || []);
    const bottomRows = limitedMoverRows(currentReport.top_losers_pipeline || currentReport.top_losers_24h_pipeline || []);
    document.getElementById("topLongTitle").textContent = `Топ роста за ${hours}ч`;
    document.getElementById("topShortTitle").textContent = `Топ падения за ${hours}ч`;
    document.getElementById("stats").innerHTML = [
      stat("Всего", summary.total_symbols),
      stat("Подходит", summary.eligible_symbols),
      stat("Выбрано", topRows.length + bottomRows.length),
      stat("Ошибки", summary.errors)
    ].join("");
    renderMoverTable("topLong", topRows, hours);
    renderMoverTable("topShort", bottomRows, hours);
  }
  renderSearchResult();
  renderPipelineBoard();
  renderBacktestTable();
}

function minVolumeUsdt() {
  const millions = Number(document.getElementById("minVolume").value || 0);
  return Math.max(0, millions) * 1_000_000;
}

function scanWindowHours() {
  const value = Number(document.getElementById("scanWindow")?.value || 24);
  return Math.min(24, Math.max(1, Math.round(value)));
}

function searchWindowHours() {
  const value = Number(document.getElementById("searchWindow")?.value || scanWindowHours());
  return Math.min(24, Math.max(1, Math.round(value)));
}

function reportWindowHours() {
  const config = currentReport?.run?.config || {};
  const summary = currentReport?.run?.summary || {};
  const value = Number(config.window_hours || summary.scan_window_hours || scanWindowHours());
  return Math.min(24, Math.max(1, Math.round(value || 24)));
}

function topLimit() {
  const fieldValue = Number(document.getElementById("maxSymbols")?.value);
  const reportValue = Number(currentReport?.run?.config?.top);
  const value = Number.isFinite(fieldValue) && fieldValue > 0 ? fieldValue : reportValue;
  return Math.max(1, Math.round(value || 5));
}

function limitedMoverRows(rows) {
  return (rows || []).slice(0, topLimit());
}

function syncScanWindowFromReport() {
  const select = document.getElementById("scanWindow");
  if (!select || !currentReport) return;
  select.value = String(reportWindowHours());
}

function stat(label, value) {
  return `<div class="stat"><strong>${value ?? 0}</strong><span>${label}</span></div>`;
}

function renderMoverTable(id, rows, hours = reportWindowHours()) {
  const sortedRows = limitedMoverRows(sortMoverRows(rows, id));
  const body = sortedRows.map((item) => {
    const metrics = marketMetrics(item);
    const symbol = item.symbol;
    const action = researchActionFor(symbol);
    return `<tr data-symbol="${symbol}">
      <td><a class="symbol-link" href="${bybitUrl(symbol)}" target="_blank" rel="noopener noreferrer" onclick="event.stopPropagation()">${symbol}</a></td>
      <td class="num">${pct(metrics.price_change_pct) || "—"}</td>
      <td class="num">${pct(metrics.volume_change_pct) || "—"}</td>
      <td class="num">${pct(metrics.funding_rate) || "н/д"}</td>
      <td class="num">${money(metrics.open_interest_value)}</td>
      <td class="num">${longShort(metrics.long_ratio, metrics.short_ratio)}</td>
      <td><button class="research-action" data-symbol="${symbol}" title="${action.title}">${action.label}</button></td>
    </tr>`;
  }).join("");
  document.getElementById(id).innerHTML = moverTableHead(hours) + `<tbody>${body || emptyRow(7)}</tbody>`;
  document.querySelectorAll(`#${id} .sort-button`).forEach((button) => {
    button.addEventListener("click", () => {
      updateSort(id, button.dataset.sort);
      renderMoverTable(id, rows, hours);
    });
  });
  document.querySelectorAll(`#${id} .research-action`).forEach((button) => {
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      const symbol = button.dataset.symbol;
      const latest = latestResearchFor(symbol);
      if (isResearchFresh(latest)) {
        selectedView = "pipeline";
        activateView("pipeline");
        render();
        openResearchCard(symbol, latest.run_id, latest.research_id);
      } else {
        runResearch(symbol, button);
      }
    });
  });
}

function moverTableHead(hours = reportWindowHours()) {
  return `<colgroup>
    <col class="col-symbol" />
    <col class="col-price" />
    <col class="col-volume" />
    <col class="col-funding" />
    <col class="col-oi" />
    <col class="col-ratio" />
    <col class="col-action" />
  </colgroup><thead><tr>
    <th>${sortButton("symbol", "Тикер")}</th>
    <th class="num" title="Изменение цены за выбранный период по Bybit.">${sortButton("price_change_pct", `Изм. цены ${hours}ч`)}</th>
    <th class="num" title="Объем за выбранный период относительно предыдущего такого же периода, оценка по часовым свечам Bybit.">${sortButton("volume_change_pct", `Изм. объема ${hours}ч`)}</th>
    <th class="num" title="Ставка финансирования Bybit для perpetual-контракта.">${sortButton("funding_rate", "Фандинг")}</th>
    <th class="num" title="Открытый интерес">${sortButton("open_interest_value", "ОИ")}</th>
    <th class="num" title="Bybit account long/short ratio: доля аккаунтов в лонге и шорте.">${sortButton("long_ratio", "Лонг / Шорт")}</th>
    <th>Действие</th>
  </tr></thead>`;
}

function renderPipelineBoard() {
  const target = document.getElementById("pipelineBoard");
  if (!target) return;
  const query = document.getElementById("pipelineSearch").value.trim().toUpperCase();
  const cards = query ? researchCards.filter((card) => String(card.symbol || "").includes(query)) : researchCards;
  target.innerHTML = cards.map((card) => researchCardHtml(card)).join("") || `
    <div class="soon">
      <h2>Карточек исследования пока нет</h2>
      <p>Запусти исследование со страницы Сканер или измени поиск по тикеру.</p>
    </div>
  `;
  document.querySelectorAll(".pipeline-card").forEach((card) => {
    card.addEventListener("click", () => openResearchCard(card.dataset.symbol, card.dataset.runId, card.dataset.researchId));
  });
}

function renderBacktestTable() {
  const target = document.getElementById("backtestTable");
  if (!target) return;
  const query = document.getElementById("backtestSearch").value.trim().toUpperCase();
  const setupFilter = document.getElementById("backtestSetupFilter").value;
  const rows = researchCards.filter((card) => {
    const matchesTicker = !query || String(card.symbol || "").includes(query);
    const matchesSetup = setupFilter === "all" || backtestSetupKind(card) === setupFilter;
    return matchesTicker && matchesSetup;
  });
  target.innerHTML = backtestTableHead() + `<tbody>${rows.map(backtestRowHtml).join("") || backtestEmptyRow()}</tbody>`;
  document.querySelectorAll("#backtestTable tbody tr[data-symbol]").forEach((row) => {
    row.addEventListener("click", () => openResearchCard(row.dataset.symbol, row.dataset.runId, row.dataset.researchId));
  });
}

function renderSearchResult() {
  const stateTarget = document.getElementById("searchState");
  const resultTarget = document.getElementById("searchResult");
  if (!stateTarget || !resultTarget) return;
  stateTarget.innerHTML = searchStatus.message
    ? `<div class="search-message ${searchStatus.state}">${escapeHtml(searchStatus.message)}</div>`
    : "";
  if (searchStatus.state === "loading") {
    resultTarget.innerHTML = "";
    return;
  }
  if (!searchResultCard) {
    resultTarget.innerHTML = `<div class="soon">
      <h2>Поиск конкретной монеты</h2>
      <p>Введите тикер или Bybit URL. Анализ сохранится в истории исследований.</p>
    </div>`;
    return;
  }
  resultTarget.innerHTML = searchResearchCardHtml(searchResultCard);
  document.querySelectorAll("#searchResult [data-open-research]").forEach((button) => {
    button.addEventListener("click", () => openResearchCard(searchResultCard.symbol, searchResultCard.run_id, searchResultCard.research_id));
  });
  hydrateSocialCharts(resultTarget);
}

function searchResearchCardHtml(card) {
  const metrics = searchQuickMetrics(card);
  const decision = normalizedDecisionLayer(card);
  const blocks = decision.blocks || {};
  const project = blocks.project || fallbackProjectBlock(card, decision);
  return `<article class="search-result-card">
    <section class="search-hero project-card-block">
      <div>
        <time>${formatDate(card.created_at)}</time>
        <h2>${escapeHtml(card.symbol || "")}</h2>
        <div class="tag-row">
          <span class="section-tag">${escapeHtml(project.tag || "Карточка проекта")}</span>
          <span class="section-tag muted">${escapeHtml(project.cvd_summary?.label || cvdMetricLabel(metrics.cvd_value, metrics.cvd_bias))}</span>
        </div>
        <p>${escapeHtml(project.project_one_liner || decision.final_decision?.no_trade_reason || decision.action || "Пайплайн завершен без итогового резюме.")}</p>
      </div>
      <div class="search-hero-side">
        <span class="verdict-badge ${decisionClass(decision.verdict || project.status)}">${escapeHtml(project.status_label || decision.final_decision?.action || decision.verdict_label || "Наблюдать")}</span>
        ${definitionList([
          ["Итог", decision.final_decision?.summary || decision.action],
          ["Сторона", sideLabel(decision.final_decision?.side || decision.preferred_side)],
          ["Что нужно", (decision.activation_triggers || []).slice(0, 2).join(" ")],
          ["CVD", project.cvd_summary?.explanation || cvdMetricNote(metrics.cvd_value, metrics.cvd_bias)]
        ])}
        <button type="button" data-open-research>Полная карточка</button>
      </div>
    </section>
    <section class="quick-metrics">
      ${quickMetric("Изм. цены", pct(metrics.price_change_pct), `${metrics.window_hours}ч`, metrics.price_change_pct)}
      ${quickMetric("Изм. объема", pct(metrics.volume_change_pct), `${metrics.window_hours}ч`, metrics.volume_change_pct, { omitIfMissing: true })}
      ${quickMetric("Объем 24ч", money(metrics.turnover_24h), "Bybit", metrics.turnover_24h)}
      ${quickMetric("Фандинг", pct(metrics.funding_rate), "бессрочный контракт", metrics.funding_rate)}
      ${quickMetric("Лонги / шорты", longShort(metrics.long_ratio, metrics.short_ratio), "соотношение позиций", metrics.long_ratio ?? metrics.short_ratio)}
      ${quickMetric("CVD", cvdMetricLabel(metrics.cvd_value, metrics.cvd_bias), "баланс покупок и продаж", metrics.cvd_value)}
    </section>
    <section class="decision-blocks">
      ${fundamentalDecisionBlock(blocks.fundamental, card.fundamentals)}
      ${socialDecisionBlock(blocks.social, card.research_charts, card.sentiment)}
      ${taDecisionBlock(blocks.ta, card.technical_analysis)}
    </section>
  </article>`;
}

function searchQuickMetrics(card) {
  const pipelineMetrics = marketMetrics(card.pipeline || {});
  const derivatives = card.technical_analysis?.metrics?.derivatives_filter?.metrics || {};
  const cvd = derivatives.cvd || {};
  return {
    window_hours: pipelineMetrics.scan_window_hours || searchWindowHours(),
    price_change_pct: pipelineMetrics.price_change_pct,
    volume_change_pct: pipelineMetrics.volume_change_pct,
    turnover_24h: pipelineMetrics.turnover_24h ?? card.pipeline?.candidate?.turnover_24h,
    funding_rate: pipelineMetrics.funding_rate ?? derivatives.funding_rate,
    open_interest: pipelineMetrics.open_interest ?? derivatives.open_interest,
    open_interest_value: pipelineMetrics.open_interest_value ?? derivatives.open_interest_value,
    long_ratio: pipelineMetrics.long_ratio ?? derivatives.long_ratio,
    short_ratio: pipelineMetrics.short_ratio ?? derivatives.short_ratio,
    long_short_status: derivatives.long_short_ratio_status,
    cvd_value: cvd.cvd_base,
    cvd_bias: cvdBiasFromValue(cvd.cvd_base)
  };
}

function quickMetric(label, value, note, rawValue, options = {}) {
  const available = rawValue !== null && rawValue !== undefined && rawValue !== "" && value && value !== "н/д";
  if (!available && options.omitIfMissing) return "";
  return `<div class="quick-card ${available ? "" : "unavailable"}">
    <span>${escapeHtml(label)}</span>
    <strong>${escapeHtml(available ? value : "нет данных")}</strong>
    <em>${escapeHtml(available ? note : "источник не вернул значение")}</em>
  </div>`;
}

function normalizedDecisionLayer(card) {
  const layer = card.decision_layer || {};
  const fallbackVerdict = card.final_verdict || card.pipeline?.candidate?.verdict || "WATCH_ONLY";
  const blocks = layer.blocks || {
    project: fallbackProjectBlock(card, layer),
    fundamental: fallbackFundamentalBlock(card.fundamentals),
    social: fallbackSocialBlock(card.sentiment, card.research_charts),
    ta: fallbackTaBlock(card.technical_analysis)
  };
  if (!blocks.project) blocks.project = fallbackProjectBlock(card, layer);
  return {
    ...layer,
    verdict: layer.verdict || fallbackVerdict,
    verdict_label: layer.verdict_label || verdictLabelRu(fallbackVerdict),
    action: layer.action || "Сделки нет; наблюдать до появления подтверждений.",
    activation_triggers: layer.activation_triggers || [],
    preferred_side: layer.preferred_side || "watch_only",
    blocks,
    final_decision: layer.final_decision || {
      action: verdictLabelRu(fallbackVerdict),
      side: layer.preferred_side || "watch_only",
      summary: layer.action || "Сделки нет; наблюдать.",
      no_trade_reason: layer.no_trade_reason || card.pipeline?.candidate?.reason_summary || "Недостаточно edge для сделки."
    }
  };
}

function fallbackProjectBlock(card, layer = {}) {
  const metrics = searchQuickMetrics(card);
  const fundamentals = card.fundamentals?.metrics || {};
  const verdict = layer.verdict || card.final_verdict || card.pipeline?.candidate?.verdict || "WATCH_ONLY";
  return {
    tag: fundamentals.fdv_tier_label || "Карточка проекта",
    status: verdict,
    status_label: verdictLabelRu(verdict),
    project_one_liner: fundamentals.project_brief_ru || fundamentals.project_summary || "",
    cvd_summary: {
      label: cvdMetricLabel(metrics.cvd_value, metrics.cvd_bias),
      bias: metrics.cvd_bias,
      explanation: cvdMetricNote(metrics.cvd_value, metrics.cvd_bias),
      value: metrics.cvd_value
    }
  };
}

function fallbackFundamentalBlock(stage) {
  const metrics = stage?.metrics || {};
  const split = fundamentalSplit(metrics);
  return {
    verdict: split.hardBlockers.length ? "risk" : "ok",
    verdict_label: split.hardBlockers.length ? "Риск" : "ОК",
    tag: split.hardBlockers.length ? "Риск supply" : "Фундаментал без красных флагов",
    status_help: "",
    summary: metrics.project_brief_ru || metrics.project_summary || "Описание проекта пока недоступно.",
    blockers: split.hardBlockers,
    reasons: split.hardBlockers.slice(0, 2),
    trade_impact: split.hardBlockers.length ? "Фундаментал требует осторожности." : "Фундаментал не мешает сделке."
  };
}

function fallbackSocialBlock(sentiment, chartsStage) {
  const metrics = sentiment?.metrics || {};
  const scenario = scenarioRu(chartsStage?.metrics?.scenario?.code);
  return {
    verdict: "watch",
    verdict_label: "Наблюдать",
    tag: scenario.label,
    scenario_label_ru: scenario.label,
    summary: scenario.summary,
    chart_explanation: chartExplanationText(),
    translated_posts: metrics.top_posts_ru || [],
    top_posts_summary_ru: metrics.top_posts_summary_ru || "",
    trade_impact: chartsStage?.metrics?.scenario?.conclusion || "Ждать подтверждения ценой и объемом.",
    velocity_ratio: metrics.social_volume_velocity_ratio,
    velocity_level: velocityLevel(metrics.social_volume_velocity_ratio),
    current_mentions: metrics.social_volume_current ?? metrics.social_volume_24h,
    baseline_mentions: metrics.social_volume_baseline,
    baseline_label: "База упоминаний",
    window_label: "Окно замера",
    window: metrics.social_volume_timeframe
  };
}

function fallbackTaBlock(technicalAnalysis) {
  const ta = technicalAnalysis?.metrics?.decision_relevant_ta || {};
  return {
    verdict: "no_confirmation",
    verdict_label: "Нет подтверждения",
    tag: "Нет подтверждения",
    strategy_label: "Нет подтверждения",
    cvd_summary: {
      label: "CVD: нет данных",
      explanation: "CVD — баланс рыночных покупок и продаж."
    },
    summary: ta.summary || "ТА пока не дала отдельного вывода.",
    supports: (ta.positives || []).map((item) => item.label || item.key),
    conflicts: (ta.negatives || []).map((item) => item.label || item.key),
    entry_conditions: ["Ждать подтверждения структуры, объема и CVD."],
    invalidation: "Отменить идею при сломе локальной структуры против выбранной стороны.",
    trade_impact: "ТА не дает достаточно сильной точки входа.",
    terms: [
      "CVD — разница рыночных покупок и продаж.",
      "ATR — текущая волатильность для оценки стопа.",
      "RSI — индикатор перегрева или перепроданности."
    ]
  };
}

function fundamentalDecisionBlock(block, stage) {
  const data = block || fallbackFundamentalBlock(stage);
  const metrics = stage?.metrics || {};
  const reasons = data.reasons?.length ? data.reasons : data.blockers?.slice(0, 2);
  const label = data.verdict_label === "Блокер" ? "Блокирующий риск" : data.verdict_label;
  const statusHelp = data.status_help || (data.verdict === "blocker" ? "Блокирующий риск — фактор, из-за которого сделку не открываем без ручной проверки." : "");
  return `<article class="decision-card fundamental">
    <div class="decision-card-head">
      <h2>Фундаментал</h2>
      <span class="badge ${decisionBadgeClass(data.verdict)}">${escapeHtml(label || "ОК")}</span>
    </div>
    <div class="tag-row"><span class="section-tag">${escapeHtml(data.tag || label || "Фундаментал")}</span></div>
    ${statusHelp ? `<p class="microcopy">${escapeHtml(statusHelp)}</p>` : ""}
    ${fundamentalMarketSnapshot(metrics)}
    <section>
      <h3>Причины</h3>
      ${bulletList(reasons?.length ? reasons : ["Критичных фундаментальных ограничений не найдено."])}
    </section>
    <p class="trade-impact">${escapeHtml(cleanTradeCopy(data.trade_impact || ""))}</p>
  </article>`;
}

function fundamentalMarketSnapshot(metrics) {
  const rows = [
    ["MC", money(metrics.market_cap)],
    ["FDV", money(metrics.fdv)],
    ["Циркуляция", ratioPct(metrics.circulating_supply_ratio)],
    ["Сектор", metrics.sector || metrics.narrative]
  ];
  const visibleRows = compactMetricRows(rows);
  if (!visibleRows.length) return "";
  return `<section class="fundamental-snapshot">
    <h3>Ключевые параметры</h3>
    ${definitionList(visibleRows)}
  </section>`;
}

function socialDecisionBlock(block, chartsStage, sentiment) {
  const data = block || fallbackSocialBlock(sentiment, chartsStage);
  const posts = russianPostList(data.translated_posts || []);
  return `<article class="decision-card social">
    <div class="decision-card-head">
      <h2>Social Intelligence</h2>
      <span class="badge ${decisionBadgeClass(data.verdict)}">${escapeHtml(data.verdict_label || "Наблюдать")}</span>
    </div>
    <div class="tag-row"><span class="section-tag social-tag">${escapeHtml(data.tag || data.scenario_label_ru || "Смешанная картина")}</span></div>
    <section>
      <h3>Обоснование</h3>
      <p>${escapeHtml(data.summary || "")}</p>
    </section>
    ${socialMetricsExplainer(data)}
    ${researchChartsSummary(chartsStage)}
    ${data.top_posts_summary_ru ? `<section><h3>Вывод по топ-постам</h3><p>${escapeHtml(data.top_posts_summary_ru)}</p></section>` : ""}
    ${posts.length ? `<section><h3>Топ-посты LunarCrush</h3>${numberedList(posts)}</section>` : ""}
    <p class="trade-impact">${escapeHtml(data.trade_impact || "Ждать подтверждения рынком.")}</p>
  </article>`;
}

function taDecisionBlock(block, technicalAnalysis) {
  const data = block || fallbackTaBlock(technicalAnalysis);
  return `<article class="decision-card ta">
    <div class="decision-card-head">
      <h2>ТА</h2>
      <span class="badge ${decisionBadgeClass(data.verdict)}">${escapeHtml(data.verdict_label || "Нет подтверждения")}</span>
    </div>
    <div class="tag-row"><span class="section-tag ta-tag">${escapeHtml(data.tag || data.strategy_label || "Торговая стратегия")}</span></div>
    <p>${escapeHtml(data.summary || "")}</p>
    ${taMarketContext(data)}
    ${data.supports?.length ? `<section><h3>Подтверждения</h3>${bulletList(data.supports)}</section>` : ""}
    ${data.conflicts?.length ? `<section><h3>Конфликты</h3>${bulletList(data.conflicts)}</section>` : ""}
    <section>
      <h3>Условия входа</h3>
      ${bulletList(data.entry_conditions || [])}
    </section>
    <p class="trade-impact">${escapeHtml(data.trade_impact || "")}</p>
    <details class="term-details">
      <summary>Расшифровка терминов</summary>
      ${bulletList(data.terms || [])}
    </details>
  </article>`;
}

function socialMetricsExplainer(data) {
  return definitionList([
    ["Упоминания", wholeNumber(data.current_mentions)],
    [data.baseline_label || "База упоминаний", wholeNumber(data.baseline_mentions)],
    ["Скорость", `${velocityRatio(data.velocity_ratio)} · ${data.velocity_level || velocityLevel(data.velocity_ratio)}`],
    [data.window_label || "Окно замера", timeframeLabel(data.window)]
  ]);
}

function verdictLabelRu(verdict) {
  return ({
    LONG_ENTER: "Лонг активен",
    LONG_WAIT_PULLBACK: "Ждать лонг",
    SHORT_ENTER: "Шорт активен",
    SHORT_WATCH: "Ждать шорт",
    WATCH_ONLY: "Наблюдать",
    AVOID: "Не торговать",
    NO_SCORE: "Нет скоринга"
  })[verdict] || "Наблюдать";
}

function sideLabel(side) {
  return ({ long: "лонг", short: "шорт", watch_only: "наблюдать" })[side] || "нейтрально";
}

function decisionBadgeClass(verdict) {
  if (["ok", "long_confirmed", "short_confirmed"].includes(verdict)) return "pass";
  if (["blocker", "conflict"].includes(verdict)) return "fail";
  return "warn";
}

function decisionClass(verdict) {
  if (["LONG_ENTER", "SHORT_ENTER", "ok", "long_confirmed", "short_confirmed"].includes(verdict)) return "pass";
  if (["AVOID", "blocker", "conflict"].includes(verdict)) return "fail";
  return "warn";
}

function taMarketContext(data) {
  const cvd = data.cvd_summary || {};
  const rows = [
    ["CVD", cvd.label],
    ["Смысл CVD", cvd.explanation]
  ];
  return `<section class="ta-market-context">${definitionList(rows)}</section>`;
}

function numberedList(items) {
  const rows = (items || []).map((item) => `<li>${escapeHtml(item)}</li>`).join("");
  return `<ol>${rows || "<li>Данных пока нет.</li>"}</ol>`;
}

function russianPostList(items) {
  return (items || []).filter((item) => /[А-Яа-яЁё]/.test(String(item || ""))).slice(0, 5);
}

function timeframeLabel(value) {
  if (!value) return "нет данных";
  if (value === "hour") return "1 час";
  return String(value).replace("h", "ч");
}

function stripProjectTaxonomy(text) {
  return String(text || "")
    .replace(/\s*Категори[яи]:[^.]+\.?/gi, "")
    .replace(/\s*Sector:[^.]+\.?/gi, "")
    .replace(/\s*Chain\/ecosystem:[^.]+\.?/gi, "")
    .trim();
}

function cleanTradeCopy(text) {
  return String(text || "")
    .replace(/пока блокер не проверен вручную/gi, "пока риск не проверен вручную")
    .replace(/блокер/gi, "блокирующий риск")
    .replace(/hard blocker/gi, "блокирующий риск");
}

function backtestTableHead() {
  return `<colgroup>
    <col class="bt-date" />
    <col class="bt-symbol" />
    <col class="bt-source" />
    <col class="bt-setup" />
    <col class="bt-risk" />
    <col class="bt-risk" />
    <col class="bt-stage" />
    <col class="bt-stage" />
    <col class="bt-rr" />
    <col class="bt-outcome" />
  </colgroup><thead><tr>
    <th>Дата</th>
    <th>Тикер</th>
    <th>Источник</th>
    <th>Сетап</th>
    <th class="num">Манипуляция</th>
    <th class="num">Late risk</th>
    <th>TA</th>
    <th>Торговый план</th>
    <th class="num">R:R</th>
    <th>Итог</th>
  </tr></thead>`;
}

function backtestRowHtml(card) {
  const symbol = card.symbol || "";
  const setupKind = backtestSetupKind(card);
  return `<tr data-symbol="${symbol}" data-run-id="${card.run_id || ""}" data-research-id="${card.research_id || ""}" title="Открыть карточку исследования">
    <td>${formatDate(card.created_at)}</td>
    <td><a class="symbol-link" href="${bybitUrl(symbol)}" target="_blank" rel="noreferrer" onclick="event.stopPropagation()">${symbol}</a></td>
    <td>${researchSourceLabel(card)}</td>
    <td>${backtestSetupLabel(card)}</td>
    <td class="num">${scoreValue(riskMetric(card, "manipulation_score"))}</td>
    <td class="num">${scoreValue(riskMetric(card, "late_entry_risk"))}</td>
    <td>${stageStatusBadge(card, "technical_analysis")}</td>
    <td>${stageStatusBadge(card, "trade_plan")}</td>
    <td class="num">${riskRewardLabel(card)}</td>
    <td>${outcomeLabel(card, setupKind)}</td>
  </tr>`;
}

function backtestEmptyRow() {
  return `<tr class="empty-state"><td colspan="10">
    <strong>Пока нет исследований</strong>
    <span>Запусти исследование в Сканере, и оно появится здесь.</span>
  </td></tr>`;
}

function backtestSetupKind(card) {
  const label = card.setup?.label;
  if (label === "Long setup") return "long";
  if (label === "Short setup") return "short";
  return "none";
}

function backtestSetupLabel(card) {
  return ({
    long: "Long",
    short: "Short",
    none: "Нет сетапа"
  })[backtestSetupKind(card)];
}

function researchSourceLabel(card) {
  const bucket = cardStage(card, "initial_selection")?.metrics?.bucket;
  const windowMatch = String(bucket || "").match(/^top_(\d+)h_(gainer|loser)$/);
  if (windowMatch) {
    return `${windowMatch[2] === "gainer" ? "Рост" : "Падение"} ${windowMatch[1]}ч`;
  }
  return ({
    top_24h_gainer: "Рост 24ч",
    top_24h_loser: "Падение 24ч",
    manual_research: "Manual"
  })[bucket] || "Manual";
}

function stageStatusBadge(card, stageName) {
  const status = cardStage(card, stageName)?.status || "skipped";
  return `<span class="badge ${status}">${statusLabel(status)}</span>`;
}

function cardStage(card, stageName) {
  return (card.pipeline?.stages || []).find((stage) => stage.stage === stageName);
}

function riskMetric(card, key) {
  const final = card.pipeline?.candidate || {};
  const manipulation = cardStage(card, "manipulation_detector")?.metrics || {};
  return final[key] ?? manipulation[key];
}

function scoreValue(value) {
  if (value === null || value === undefined || value === "") return "—";
  return Number(value).toFixed(1);
}

function riskRewardLabel(card) {
  const rr = card.setup?.trade_plan?.risk_reward;
  if (rr === null || rr === undefined || rr === "") return "—";
  return Number(rr).toFixed(2);
}

function outcomeLabel(card, setupKind) {
  if (setupKind === "none" || !card.setup?.trade_plan) return "Нет сетапа";
  return "Ждет проверки";
}

function researchCardHtml(card) {
  const pipeline = card.pipeline || {};
  const stages = pipeline.stages || [];
  const decision = normalizedDecisionLayer(card);
  const blocks = decision.blocks || {};
  const tags = [blocks.project?.tag, blocks.fundamental?.tag, blocks.social?.tag || blocks.social?.scenario_label_ru, blocks.ta?.tag]
    .filter(Boolean)
    .slice(0, 4);
  return `<article class="pipeline-card" data-symbol="${card.symbol}" data-run-id="${card.run_id || ""}" data-research-id="${card.research_id || ""}">
    <div class="pipeline-header">
      <div>
        <time>${formatDate(card.created_at)}</time>
        <strong>${card.symbol}</strong>
      </div>
    </div>
    ${tags.length ? `<div class="pipeline-tags">${tags.map((tag) => `<span>${escapeHtml(tag)}</span>`).join("")}</div>` : ""}
    <div class="pipeline-badges">${pipelineBadgeList(stages)}</div>
  </article>`;
}

function openResearchCard(symbol, runId = "", researchId = "") {
  const card = researchCardByIdentity(symbol, runId, researchId);
  if (!card) return;
  const pipeline = card.pipeline || {};
  const stages = stageOrder.map((name) => (pipeline.stages || []).find((stage) => stage.stage === name)).filter(Boolean);
  document.getElementById("drawerContent").innerHTML = `
    <h1>${card.symbol}</h1>
    ${searchResearchCardHtml(card)}
    <details class="audit-details">
      <summary>Технические детали</summary>
      <h2>Ссылки</h2>
      <div class="link-list">
        ${Object.entries(card.links || {}).map(([label, url]) => `<a href="${url}" target="_blank" rel="noreferrer">${label}</a>`).join("")}
      </div>
      <h2>Пайплайн</h2>
      <div class="timeline">${stages.map(stageBlock).join("")}</div>
      <h2>JSON сетапа</h2>
      <pre>${escapeHtml(JSON.stringify(localizedSetup(card.setup || {}), null, 2))}</pre>
    </details>
  `;
  document.getElementById("drawer").classList.add("open");
  hydrateSocialCharts(document.getElementById("drawerContent"));
}

function decisionLayerSummary(layer) {
  const data = layer || {};
  const derivatives = data.derivatives || {};
  const fundamentals = data.fundamentals || {};
  const ta = data.ta || {};
  const rows = [
    ["Вердикт", data.verdict_label || data.verdict],
    ["Действие", data.action],
    ["Почему нет сделки", data.no_trade_reason],
    ["Главный риск", data.primary_risk ? `${data.primary_risk.label}: ${data.primary_risk.reason}` : null],
    ["Next step", data.chart_next_step]
  ];
  const derivativeRows = [
    ["CVD", derivatives.cvd_status === "available" ? `${metricNumber(derivatives.cvd_base)} (${cvdBiasLabel(derivatives.cvd_bias)})` : "нет данных"],
    ["Конфликт CVD", derivatives.cvd_conflict ? derivatives.cvd_conflict_reason : "нет"],
    ["Фандинг", pct(derivatives.funding_rate)],
    ["Лонги / шорты", longShort(derivatives.long_ratio, derivatives.short_ratio)]
  ];
  return `<div class="decision-layer">
    ${definitionList(rows)}
    <section>
      <h3>Триггеры активации</h3>
      ${bulletList(data.activation_triggers || [])}
    </section>
    <section>
      <h3>Derivatives / CVD</h3>
      ${definitionList(derivativeRows)}
    </section>
    <section>
      <h3>Fundamentals split</h3>
      <div class="split-grid">
        <div>
          <h4>Hard blockers</h4>
          ${bulletList(fundamentals.hard_blockers?.length ? fundamentals.hard_blockers : ["Критичных hard blockers нет."])}
        </div>
        <div>
          <h4>Context only</h4>
          ${bulletList(fundamentals.context_only || [])}
        </div>
      </div>
    </section>
    <section>
      <h3>Decision-relevant TA</h3>
      <p>${escapeHtml(ta.summary || "Нет агрегированного TA вывода.")}</p>
      ${definitionList([
        ["Bias", ta.preferred_side],
        ["TA decision score", metricNumber(ta.decision_score)],
        ["Positive / negative", `${metricNumber(ta.positive_score)} / ${metricNumber(ta.negative_score)}`]
      ])}
      <div class="split-grid">
        <div>
          <h4>Поддерживает</h4>
          ${weightedSignalList(ta.positives)}
        </div>
        <div>
          <h4>Мешает</h4>
          ${weightedSignalList(ta.negatives)}
        </div>
      </div>
    </section>
  </div>`;
}

function stageSummary(stage, blockingStage = null) {
  if (stage?.stage === "research_charts") return researchChartsSummary(stage);
  if (stage?.stage === "manipulation_detector") return manipulationSummary(stage);
  if (stage?.metrics?.fundamental_label) return fundamentalSummary(stage);
  if (stage?.stage === "social_filter" || stage?.metrics?.social_label) return socialSummary(stage);
  const reason = skippedBecauseOfBlockingStage(stage, blockingStage)
    ? `Этап не запускался, потому что пайплайн остановился раньше: ${stageLabel(blockingStage.stage)} — ${stageReason(blockingStage)}`
    : stageReason(stage);
  return `<div class="stage-summary">
    <span class="badge ${stage?.status || "skipped"}">${stageResultLabel(stage)}</span>
    <p>${escapeHtml(reason || "Этап ещё не запускался.")}</p>
    ${stageMetricList(stage)}
  </div>`;
}

function researchChartsSummary(stage) {
  const metrics = stage?.metrics || {};
  return `<div class="stage-summary chart-summary">
    <div class="chart-window-meta">
      <span>Окно: ${escapeHtml(metrics.window_hours ? `${metrics.window_hours}ч` : "нет данных")}</span>
      <span>Начало: ${escapeHtml(chartEdgeTime(metrics, "first") || "—")}</span>
      <span>Конец: ${escapeHtml(chartEdgeTime(metrics, "last") || "—")}</span>
    </div>
    ${overlayChart(metrics)}
  </div>`;
}

function overlayChart(metrics) {
  const points = metrics?.indexed_points || [];
  const valid = points.filter((point) => ["mentions", "price", "volume"].some((key) => Number.isFinite(Number(point?.[key]))));
  if (!points.length || !valid.length) {
    return `<section class="overlay-chart">
      <div class="chart-empty">${escapeHtml(metrics?.scenario?.conclusion || "Нет данных для нормализованного графика")}</div>
    </section>`;
  }
  const width = 620;
  const height = 220;
  const padX = 38;
  const padY = 22;
  const plotWidth = width - padX * 2;
  const plotHeight = height - padY * 2;
  const series = [
    ["mentions", "Упоминания", "mentions"],
    ["price", "Цена", "price"],
    ["volume", "Объем", "volume"]
  ];
  const payload = chartPointPayload(metrics, points);
  const xFor = (index) => padX + (index * plotWidth) / Math.max(1, points.length - 1);
  const yFor = (value) => height - padY - (Math.max(0, Math.min(100, value)) / 100) * plotHeight;
  const lines = series.map(([key, label, className]) => {
    const coords = points.map((point, index) => {
      const value = Number(point?.[key]);
      if (!Number.isFinite(value)) return null;
      return `${xFor(index).toFixed(1)},${yFor(value).toFixed(1)}`;
    }).filter(Boolean);
    if (!coords.length) return "";
    return `<polyline class="overlay-line ${className}" data-series="${className}" points="${coords.join(" ")}"><title>${escapeHtml(label)}</title></polyline>`;
  }).join("");
  const hoverPoints = payload.map((point, index) => {
    const x = xFor(index).toFixed(1);
    const circles = series.map(([key, label, className]) => {
      const value = Number(points[index]?.[key]);
      if (!Number.isFinite(value)) return "";
      return `<circle class="chart-hotspot ${className}" data-series="${className}" data-point="${index}" cx="${x}" cy="${yFor(value).toFixed(1)}" r="9">
        <title>${escapeHtml(label)} · ${escapeHtml(point.time_label)} · ${metricNumber(value)}</title>
      </circle>`;
    }).join("");
    return circles;
  }).join("");
  const events = metrics?.events || {};
  const markers = [
    ["mentions_event", "M", "mentions"],
    ["price_event", "P", "price"],
    ["volume_event", "V", "volume"]
  ].map(([key, label, className]) => {
    const event = events[key];
    const index = Number(event?.index);
    if (!Number.isFinite(index)) return "";
    const x = xFor(index);
    return `<g class="event-marker ${className}">
      <line x1="${x.toFixed(1)}" y1="${padY}" x2="${x.toFixed(1)}" y2="${height - padY}"></line>
      <text x="${x.toFixed(1)}" y="${padY - 6}" text-anchor="middle">${label}</text>
      <title>${escapeHtml(eventLabel(key))}: ${escapeHtml(formatDate(event.time))}</title>
    </g>`;
  }).join("");
  return `<section class="overlay-chart">
    <div class="overlay-chart-head">
      <h3>Social Intelligence</h3>
      <div class="overlay-legend">
        ${series.map(([, label, className]) => `<button type="button" class="${className}" data-chart-toggle="${className}"><i></i>${label}</button>`).join("")}
      </div>
    </div>
    <div class="chart-canvas" data-chart-points="${escapeHtml(JSON.stringify(payload))}">
      <svg viewBox="0 0 ${width} ${height}" role="img" aria-label="Нормализованный график упоминаний, цены и объема">
      <line x1="${padX}" y1="${padY}" x2="${padX}" y2="${height - padY}" class="chart-axis"></line>
      <line x1="${padX}" y1="${height - padY}" x2="${width - padX}" y2="${height - padY}" class="chart-axis"></line>
      <line x1="${padX}" y1="${height / 2}" x2="${width - padX}" y2="${height / 2}" class="chart-zero"></line>
      <line class="chart-crosshair" x1="${padX}" y1="${padY}" x2="${padX}" y2="${height - padY}"></line>
      ${lines}
      ${markers}
      ${hoverPoints}
      </svg>
      <div class="chart-tooltip" hidden></div>
    </div>
    <div class="event-strip">
      ${eventStripItem("M: всплеск упоминаний", events.mentions_event)}
      ${eventStripItem("P: всплеск цены", events.price_event)}
      ${eventStripItem("V: всплеск объема", events.volume_event)}
    </div>
    <p class="chart-now">Сейчас: ${escapeHtml(currentChartState(metrics))}</p>
  </section>`;
}

function chartPointPayload(metrics, indexedPoints) {
  const charts = metrics?.charts || {};
  const mentions = charts.mentions?.points || [];
  const price = charts.price_change?.points || [];
  const volume = charts.volume_change?.points || [];
  return (indexedPoints || []).map((point, index) => ({
    time: point.time,
    time_label: formatDate(point.time),
    mentions_norm: point.mentions,
    price_norm: point.price,
    volume_norm: point.volume,
    mentions_raw: mentions[index]?.value,
    price_raw: price[index]?.value,
    volume_raw: volume[index]?.value
  }));
}

function currentChartState(metrics) {
  const scenario = scenarioRu(metrics?.scenario?.code);
  const events = metrics?.events || {};
  const order = [
    events.mentions_event ? ["соцсигнал впереди", Number(events.mentions_event.index)] : null,
    events.price_event ? ["цена впереди", Number(events.price_event.index)] : null,
    events.volume_event ? ["объем подтвердил", Number(events.volume_event.index)] : null
  ].filter((item) => item && Number.isFinite(item[1])).sort((a, b) => a[1] - b[1]);
  if (order.length) return `${order[0][0]}; сценарий: ${scenario.label}.`;
  return `подтверждения нет; сценарий: ${scenario.label}.`;
}

function hydrateSocialCharts(root = document) {
  root.querySelectorAll(".overlay-chart").forEach((chart) => {
    const canvas = chart.querySelector(".chart-canvas");
    const svg = chart.querySelector("svg");
    const tooltip = chart.querySelector(".chart-tooltip");
    const crosshair = chart.querySelector(".chart-crosshair");
    if (!canvas || !svg || !tooltip || chart.dataset.hydrated === "1") return;
    chart.dataset.hydrated = "1";
    let points = [];
    try {
      points = JSON.parse(canvas.dataset.chartPoints || "[]");
    } catch {
      points = [];
    }
    chart.querySelectorAll("[data-chart-toggle]").forEach((button) => {
      button.addEventListener("click", () => {
        const series = button.dataset.chartToggle;
        const hidden = button.classList.toggle("off");
        button.classList.toggle("off", hidden);
        chart.querySelectorAll(`[data-series="${series}"]`).forEach((node) => {
          node.classList.toggle("series-hidden", hidden);
        });
      });
    });
    chart.querySelectorAll(".chart-hotspot").forEach((hotspot) => {
      const show = () => showChartPoint(points, Number(hotspot.dataset.point), hotspot, tooltip, crosshair);
      hotspot.addEventListener("mouseenter", show);
      hotspot.addEventListener("focus", show);
      hotspot.addEventListener("click", show);
      hotspot.addEventListener("mouseleave", () => hideChartTooltip(tooltip, crosshair));
      hotspot.setAttribute("tabindex", "0");
    });
  });
}

function showChartPoint(points, index, hotspot, tooltip, crosshair) {
  const point = points[index] || {};
  const x = hotspot.getAttribute("cx") || "0";
  crosshair.setAttribute("x1", x);
  crosshair.setAttribute("x2", x);
  crosshair.classList.add("active");
  tooltip.hidden = false;
  tooltip.style.left = `${Math.min(78, Math.max(10, (Number(x) / 620) * 100))}%`;
  tooltip.innerHTML = `
    <strong>${escapeHtml(point.time_label || "Нет времени")}</strong>
    <span>Упоминания: ${escapeHtml(chartMetricPair(point.mentions_norm, point.mentions_raw, "mentions"))}</span>
    <span>Цена: ${escapeHtml(chartMetricPair(point.price_norm, point.price_raw, "price"))}</span>
    <span>Объем: ${escapeHtml(chartMetricPair(point.volume_norm, point.volume_raw, "volume"))}</span>
  `;
}

function hideChartTooltip(tooltip, crosshair) {
  tooltip.hidden = true;
  crosshair.classList.remove("active");
}

function chartMetricPair(norm, raw, kind) {
  const normalized = Number.isFinite(Number(norm)) ? `${Number(norm).toFixed(0)}/100` : "—";
  let rawLabel = "—";
  if (Number.isFinite(Number(raw))) {
    rawLabel = kind === "mentions" ? wholeNumber(raw) : pct(raw);
  }
  return `${normalized}; исходное: ${rawLabel}`;
}

function eventStripItem(label, event) {
  if (!event) return `<span>${escapeHtml(label)}: —</span>`;
  return `<span>${escapeHtml(label)}: ${escapeHtml(formatDate(event.time))}</span>`;
}

function eventLabel(key) {
  return ({
    mentions_event: "Всплеск упоминаний",
    price_event: "Всплеск цены",
    volume_event: "Всплеск объема",
    oi_event: "Всплеск OI"
  })[key] || key;
}

function chartEdgeTime(metrics, edge) {
  const points = metrics?.indexed_points || [];
  const point = edge === "first" ? points[0] : points[points.length - 1];
  return point?.time ? formatDate(point.time) : "";
}

function chartExplanationText() {
  return "Все линии нормализованы к шкале 0-100 внутри выбранного окна; это не цена в USDT и не абсолютный объем. Горизонтальная ось — часы. M/P/V отмечают первый значимый всплеск упоминаний, цены и объема.";
}

function scenarioRu(code) {
  return ({
    early_narrative: {
      label: "Ранний соцсигнал",
      summary: "Упоминания растут раньше цены и объема. Это сигнал для наблюдения, вход только после подтверждения рынком."
    },
    narrative: {
      label: "Подтвержденный нарратив",
      summary: "Упоминания пришли первыми, затем цена и объем подтвердили движение."
    },
    exhaustion_late_hype: {
      label: "Поздний хайп",
      summary: "Цена уже прошла движение, а соцсети догоняют. Риск позднего входа высокий."
    },
    fake_pump: {
      label: "Подозрительный памп",
      summary: "Цена двинулась без нормального подтверждения объемом и соцсетями."
    },
    insider_pump: {
      label: "Цена раньше соцсетей",
      summary: "Цена и объем сдвинулись раньше публичного внимания. Нужен ретест, без FOMO."
    },
    insufficient_social_data: {
      label: "Мало соцданных",
      summary: "LunarCrush не дал достаточную часовую историю; соцблок не повышает уверенность."
    },
    insufficient_market_data: {
      label: "Мало рыночных данных",
      summary: "Bybit-истории недостаточно для честного сравнения цены, объема и упоминаний."
    },
    mixed: {
      label: "Смешанная картина",
      summary: "Нет чистого лидерства соцсетей, цены или объема. Нужны дополнительные подтверждения."
    }
  })[code || "mixed"] || {
    label: "Смешанная картина",
    summary: "Нет чистого лидерства соцсетей, цены или объема. Нужны дополнительные подтверждения."
  };
}

function lineChart(chart, kind) {
  const points = chart?.points || [];
  const values = points.map((point) => Number(point?.value)).filter(Number.isFinite);
  const status = chart?.status || "нет данных";
  if (!points.length || !values.length) {
    return `<section class="mini-chart ${kind}">
      <h3>${escapeHtml(chart?.label || kind)}</h3>
      <div class="chart-empty">${escapeHtml(chart?.reason || status || "No data")}</div>
    </section>`;
  }
  const width = 260;
  const height = 96;
  const pad = 10;
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min || 1;
  const coords = points.map((point, index) => {
    const value = Number(point?.value);
    const x = pad + (index * (width - pad * 2)) / Math.max(1, points.length - 1);
    if (!Number.isFinite(value)) return null;
    const y = height - pad - ((value - min) / span) * (height - pad * 2);
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  }).filter(Boolean);
  const latest = points.slice().reverse().find((point) => Number.isFinite(Number(point?.value)))?.value;
  const latestLabel = chart.unit === "pct" ? pct(latest) : wholeNumber(latest);
  return `<section class="mini-chart ${kind}">
    <h3>${escapeHtml(chart.label || kind)} <span>${escapeHtml(latestLabel || "")}</span></h3>
    <svg viewBox="0 0 ${width} ${height}" role="img" aria-label="${escapeHtml(chart.label || kind)}">
      <line x1="${pad}" y1="${height / 2}" x2="${width - pad}" y2="${height / 2}" class="chart-zero"></line>
      <polyline points="${coords.join(" ")}"></polyline>
    </svg>
  </section>`;
}

function cvdBiasLabel(value) {
  return ({ positive: "покупатель сильнее", negative: "продавец сильнее", neutral: "нейтрально" })[value] || "нейтрально";
}

function socialSummary(stage) {
  const metrics = stage?.metrics || {};
  const volumeRows = [
    ["Упоминания", wholeNumber(metrics.social_volume_current ?? metrics.social_volume_24h)],
    ["База", wholeNumber(metrics.social_volume_baseline)],
    ["Предыдущее значение", wholeNumber(metrics.social_volume_previous)],
    ["Скорость", velocityRatio(metrics.social_volume_velocity_ratio)],
    ["Отклонение", pctFromPercent(metrics.social_volume_velocity_pct)],
    ["Окно", metrics.social_volume_timeframe],
    ["Источник", metrics.social_volume_source || metrics.data_coverage]
  ];
  const contextRows = [
    ["Почему заметили", metrics.why_moved],
    ["Сигналы", metrics.alerts || metrics.supportive_causes],
  ];
  return `<div class="stage-summary social-summary">
    <span class="badge ${stage?.status || "skipped"}">${stageResultLabel(stage)}</span>
    <p>${escapeHtml(stageReason(stage) || "Этап еще не запускался.")}</p>
    <section>
      <h3>Скорость упоминаний</h3>
      ${definitionList(volumeRows)}
    </section>
    <section>
      <h3>Контекст LunarCrush</h3>
      ${definitionList(contextRows)}
    </section>
  </div>`;
}

function manipulationSummary(stage) {
  const metrics = stage?.metrics || {};
  const score = Number(metrics.manipulation_score);
  const late = Number(metrics.late_entry_risk);
  const riskText = Number.isFinite(score) ? `${score.toFixed(2)} / 100 — ${riskLevel(score)}` : "нет данных";
  const lateText = Number.isFinite(late) ? `${late.toFixed(2)} / 100 — ${lateEntryLevel(late)}` : "нет данных";
  const riskSummaryRows = [
    ["Риск ликвидности / манипулятивности", riskText],
    ["Риск позднего входа", lateText],
    ["Шкала", "<=55 пройдено; 56-82 предупреждение; >82 не пройдено"]
  ];
  const activeManipulation = activeRiskFactors(metrics.manipulation_breakdown);
  const activeLateEntry = activeRiskFactors(metrics.late_entry_breakdown);
  const contextFlags = cleanList(metrics.risk_contributors).filter((item) => item !== "критичных факторов в доступных данных нет");
  return `<div class="stage-summary manipulation-summary">
    <span class="badge ${stage?.status || "skipped"}">${stageResultLabel(stage)}</span>
    <p>${escapeHtml(stageReason(stage) || "Этап еще не запускался.")}</p>
    ${definitionList(riskSummaryRows)}
    <section>
      <h3>Что добавило риск манипулятивности</h3>
      ${riskFactorList(activeManipulation, "Существенных факторов в формуле не сработало.")}
    </section>
    <section>
      <h3>Что добавило риск позднего входа</h3>
      ${riskFactorList(activeLateEntry, "Существенных факторов позднего входа не сработало.")}
    </section>
    <section>
      <h3>Контекстные флаги</h3>
      ${bulletList(contextFlags.length ? contextFlags : ["Критичных контекстных флагов нет."])}
    </section>
  </div>`;
}

function technicalAnalysisSummary(technicalAnalysis, fallbackStrategy) {
  const stage = technicalAnalysis?.stage || {};
  const metrics = technicalAnalysis?.metrics || {};
  const decisionTa = metrics.decision_relevant_ta || {};
  const derivatives = metrics.derivatives_filter?.metrics || {};
  const cvd = derivatives.cvd || {};
  const derivativeRows = [
    ["Фандинг", pct(derivatives.funding_rate) || "—"],
    ["Лонги / шорты", longShort(derivatives.long_ratio, derivatives.short_ratio)],
    ["CVD", cvd.status === "available" ? metricNumber(cvd.cvd_base) : "нет данных"]
  ];
  const execution = metrics.execution_context || {};
  return `<div class="stage-summary technical-summary">
    <span class="badge ${stage.status || "skipped"}">${stageResultLabel(stage)}</span>
    <p>${escapeHtml(metrics.principle || "Derivatives/market metrics определяют что торговать; ТА определяет когда и где входить.")}</p>
    <section>
      <h3>Рыночные подтверждения</h3>
      ${definitionList(derivativeRows)}
    </section>
    <section>
      <h3>Сигналы ТА</h3>
      ${decisionTa.summary ? `<p>${escapeHtml(decisionTa.summary)}</p>` : ""}
      ${decisionTa.positives || decisionTa.negatives ? `<div class="split-grid">
        <div>
          <h4>Поддерживает</h4>
          ${weightedSignalList(decisionTa.positives)}
        </div>
        <div>
          <h4>Мешает</h4>
          ${weightedSignalList(decisionTa.negatives)}
        </div>
      </div>` : ""}
    </section>
    <section>
      <h3>Исполнение</h3>
      ${definitionList([
        ["Вход", execution.entry_basis || "Подтверждение ТА"],
        ["Стоп", execution.stop_loss_basis || "ATR / структура"],
        ["Цели", execution.take_profit_basis || "структура / ликвидность"]
      ])}
    </section>
  </div>`;
}

function activeRiskFactors(items) {
  return (items || []).filter((item) => Number(item?.points || 0) > 0);
}

function riskFactorList(items, fallback) {
  if (!items.length) return bulletList([fallback]);
  return `<ul>${items.map((item) => `<li><strong>${escapeHtml(item.label || item.key)}</strong>: +${metricNumber(item.points)} из ${metricNumber(item.max_points)}. ${escapeHtml(formatRiskValue(item))}${item.description ? ` ${escapeHtml(item.description)}` : ""}</li>`).join("")}</ul>`;
}

function formatRiskValue(item) {
  const value = item?.value;
  if (item?.value_type === "ratio") return `Значение: ${ratioPct(value) || "н/д"}.`;
  if (item?.value_type === "USDT") return `Значение: ${money(value) || "н/д"}.`;
  if (item?.value_type === "bps") return `Значение: ${metricNumber(value)} bps.`;
  if (item?.value_type === "bool") return `Значение: ${value ? "да" : "нет"}.`;
  return `Значение: ${metricNumber(value)}.`;
}

function riskLevel(score) {
  if (score > 82) return "высокий";
  if (score > 55) return "средний";
  return "низкий";
}

function lateEntryLevel(score) {
  if (score > 75) return "движение перегрето";
  if (score > 45) return "есть риск догонять движение";
  return "вход не выглядит поздним";
}

function skippedBecauseOfBlockingStage(stage, blockingStage) {
  if (!stage || !blockingStage) return false;
  if (stage.stage === blockingStage.stage) return false;
  return stage.status === "skipped" || stage.status === undefined;
}

function fundamentalSummary(stage) {
  const metrics = stage?.metrics || {};
  const split = fundamentalSplit(metrics);
  const sizeRows = [
    ["FDV", metrics.fdv_tier_label || metrics.market_cap_tier_label || "нет данных"],
    ["FDV", money(metrics.fdv) || "—"],
    ["Капитализация", money(metrics.market_cap) || "—"],
    ["MC / FDV", ratioPct(metrics.market_cap_to_fdv_ratio) || "—"],
    ["Supply", metrics.market_cap_to_fdv_label]
  ];
  return `<div class="stage-summary fundamental-summary">
    <span class="badge ${stage?.status || "skipped"}">${stageResultLabel(stage)}</span>
    <div class="fundamental-card">
      <section>
        <h3>Описание проекта</h3>
        <p>${escapeHtml(stripProjectTaxonomy(metrics.project_brief_ru || metrics.project_summary || "Данных пока нет."))}</p>
      </section>
      <section>
        <h3>Ограничения</h3>
        ${bulletList(split.hardBlockers.length ? split.hardBlockers : ["Критичных блокеров нет."])}
      </section>
      <section>
        <h3>Размер и supply</h3>
        ${definitionList(sizeRows)}
      </section>
    </div>
  </div>`;
}

function fundamentalSplit(metrics) {
  const hardBlockers = [];
  const unlock = String(metrics.unlock_risk_label || "");
  if (unlock.toLowerCase().includes("есть")) hardBlockers.push("Unlock/vesting упоминается публично: нужна ручная проверка даты и размера.");
  if (Number(metrics.tokenomics_risk_score) >= 65) hardBlockers.push("Tokenomics risk высокий.");
  if (Number(metrics.circulating_supply_ratio) < 0.30) hardBlockers.push("Циркуляция ниже 30% от общего или максимального предложения.");
  if (["tiny", "giant"].includes(String(metrics.fdv_tier || ""))) hardBlockers.push(`Extreme FDV tier: ${metrics.fdv_tier_label || metrics.fdv_tier}.`);
  cleanList(metrics.red_flags).forEach((flag) => {
    const lowered = flag.toLowerCase();
    if (["scam", "rug", "blacklist"].some((term) => lowered.includes(term))) hardBlockers.push(flag);
  });
  const categories = Array.isArray(metrics.categories) ? metrics.categories.slice(0, 5).join(", ") : metrics.categories;
  const contextOnly = [
    metrics.sector ? `Sector: ${metrics.sector}` : "",
    metrics.chain_ecosystem ? `Chain/ecosystem: ${metrics.chain_ecosystem}` : "",
    categories ? `Categories: ${categories}` : "",
    metrics.project_brief_ru || metrics.project_summary || ""
  ].filter(Boolean);
  return { hardBlockers, contextOnly };
}

function weightedSignalList(items) {
  const rows = (items || []).map((item) => `<li><strong>${escapeHtml(item.label || item.key)}</strong>: вес ${metricNumber(item.weight)}</li>`).join("");
  return `<ul class="weighted-list">${rows || "<li>Нет сильных торговых сигналов.</li>"}</ul>`;
}

function bulletList(items) {
  const rows = (items || []).map((item) => `<li>${escapeHtml(item)}</li>`).join("");
  return `<ul>${rows || "<li>Данных пока нет.</li>"}</ul>`;
}

function cleanList(items) {
  return (items || []).map((item) => String(item || "").trim()).filter(Boolean);
}

function stageBlock(stage) {
  const reason = stageReason(stage);
  return `<div class="stage">
    <div>${stageLabel(stage.stage)}</div>
    <div><span class="badge ${stage.status}">${statusLabel(stage.status)}</span></div>
    <div>
      <div>${escapeHtml(reason)}</div>
      <pre>${escapeHtml(JSON.stringify({ score: stage.score, blocking: stage.blocking, metrics: displayMetrics(stage.metrics) }, null, 2))}</pre>
    </div>
  </div>`;
}

function stageReason(stage) {
  if (!stage) return "";
  if (stage.stage === "market_scan" && stage.status === "fail") {
    return marketFilterReason(stage.metrics || {}, stage.reason || "");
  }
  return translateReason(stage.reason || "");
}

function marketFilterReason(metrics, fallback) {
  const filterReasons = Array.isArray(metrics.filter_reasons) ? metrics.filter_reasons : [];
  const translated = filterReasons.map(translateFilterReason).filter(Boolean);
  if (translated.length) return `Монета не прошла базовый фильтр. ${translated.join(" ")}`;

  const turnover = Number(metrics.turnover_24h);
  const minimum = Number(metrics.min_turnover_24h);
  if (Number.isFinite(turnover) && Number.isFinite(minimum) && turnover < minimum) {
    return `Монета не прошла базовый фильтр. Объем 24ч ${money(turnover)} ниже минимума ${money(minimum)}.`;
  }

  const translatedFallback = translateReason(fallback);
  if (translatedFallback) return translatedFallback;
  return "Монета не прошла базовые фильтры перед глубоким исследованием.";
}

function translateFilterReason(reason) {
  const text = String(reason || "");
  const turnoverMatch = text.match(/24h turnover \$([\d,]+) is below minimum \$([\d,]+)\./);
  if (turnoverMatch) {
    return `Объем 24ч $${turnoverMatch[1]} ниже минимума $${turnoverMatch[2]}.`;
  }
  const spreadMatch = text.match(/Ticker spread ([\d.]+) bps is above 60\.0 bps\./);
  if (spreadMatch) return `Спред ${spreadMatch[1]} bps выше лимита 60 bps.`;
  if (text === "Base coin is excluded by universe filter.") return "Базовая монета исключена фильтром universe.";
  if (text === "Quote coin is not USDT.") return "Пара не в USDT.";
  if (text === "Ticker is missing.") return "Нет ticker-данных Bybit.";
  if (text === "Instrument not found.") return "Инструмент не найден в Bybit.";
  if (text === "Last price is missing or zero.") return "Последняя цена отсутствует или равна нулю.";
  if (text.startsWith("Instrument status is ")) return `Статус инструмента: ${text.replace("Instrument status is ", "").replace(".", "")}.`;
  if (text.startsWith("Contract type is ")) return `Тип контракта: ${text.replace("Contract type is ", "").replace(".", "")}.`;
  return text;
}

function translateReason(reason) {
  const text = String(reason || "");
  if (!text) return "";
  if (text === "Symbol failed universe/liquidity filters before expensive analysis.") {
    return "Монета не прошла базовые фильтры перед глубоким исследованием.";
  }
  if (text === "Symbol is tradable and eligible for high-volatility prefiltering.") {
    return "Монета торгуется и подходит для первичного high-volatility отбора.";
  }
  if (text === "Stage has not run yet.") return "Этап еще не запускался.";
  if (text === "No actionable setup was produced by the trade plan stage.") {
    return "Торговый план не сформировал рабочий сетап.";
  }
  return text;
}

function displayMetrics(metrics) {
  const result = { ...(metrics || {}) };
  if (result.verdict === "WATCH_ONLY") {
    result.research_result = "Нет торгового сетапа";
    delete result.verdict;
  }
  delete result.blacklist_label;
  delete result.blacklist_risk_score;
  delete result.hits;
  delete result.public_mentions;
  return result;
}

function marketMetrics(item) {
  const selection = selectionStage(item);
  const market = (item.stages || []).find((stage) => stage.stage === "market_scan")?.metrics || {};
  const rawTicker = (item.raw_snapshots || []).find((snapshot) => snapshot.source === "bybit.ticker")?.payload || {};
  const windowHours = Number(selection.scan_window_hours || reportWindowHours());
  const price24h = selection.price_24h_pct ?? market.price_24h_pct ?? rawTicker.price_24h_pct;
  const volumeChange24h = selection.volume_change_24h_pct;
  return {
    scan_window_hours: windowHours,
    price_change_pct: selection.price_change_window_pct ?? price24h,
    volume_change_pct: selection.volume_change_window_pct ?? volumeChange24h,
    price_24h_pct: price24h,
    volume_change_24h_pct: volumeChange24h,
    turnover_24h: selection.turnover_24h ?? market.turnover_24h ?? rawTicker.turnover_24h,
    funding_rate: selection.funding_rate ?? market.funding_rate ?? rawTicker.funding_rate,
    open_interest: selection.open_interest ?? market.open_interest ?? rawTicker.open_interest,
    open_interest_value: selection.open_interest_value ?? market.open_interest_value ?? rawTicker.open_interest_value,
    long_ratio: selection.long_ratio,
    short_ratio: selection.short_ratio
  };
}

function sortMoverRows(rows, tableId) {
  const state = sortState[tableId];
  return [...rows].sort((left, right) => {
    const a = sortableValue(left, state.key);
    const b = sortableValue(right, state.key);
    const result = typeof a === "string" ? a.localeCompare(String(b)) : numberSort(a, b);
    return state.direction === "asc" ? result : -result;
  });
}

function sortableValue(item, key) {
  if (key === "symbol") return item.symbol || "";
  return Number(marketMetrics(item)[key] ?? Number.NEGATIVE_INFINITY);
}

function numberSort(a, b) {
  if (!Number.isFinite(a) && !Number.isFinite(b)) return 0;
  if (!Number.isFinite(a)) return -1;
  if (!Number.isFinite(b)) return 1;
  return a - b;
}

function updateSort(tableId, key) {
  const state = sortState[tableId];
  if (state.key === key) {
    state.direction = state.direction === "asc" ? "desc" : "asc";
  } else {
    state.key = key;
    state.direction = key === "symbol" ? "asc" : "desc";
  }
}

function sortButton(key, label) {
  return `<button class="sort-button" data-sort="${key}" type="button">${label}</button>`;
}

function pipelineBadgeList(stages) {
  const badgeMap = [
    ["Волатильность", "market_scan"],
    ["Charts", "research_charts"],
    ["Фундаментал", "fundamentals"],
    ["Соцфильтр", "social_filter"],
    ["Манипуляторы", "manipulation_detector"],
    ["Теханализ", "technical_analysis"],
    ["Торговый план", "trade_plan"],
    ["Резюме", "final_ranking"]
  ];
  return badgeMap.map(([label, stageName]) => {
    const stage = stages.find((item) => item.stage === stageName) || { status: "skipped", reason: "" };
    return `<span class="pipeline-badge ${stage.status}" title="${escapeHtml(stage.reason || "")}">
      <b>${label}</b>
      <em>${stageResultLabel(stage)}</em>
    </span>`;
  }).join("");
}

function selectionStage(item) {
  const stage = (item.stages || []).find((entry) => entry.stage === "initial_selection");
  return stage ? (stage.metrics || {}) : {};
}

function activateView(viewName) {
  selectedView = viewName;
  document.querySelectorAll(".nav-item").forEach((item) => item.classList.toggle("active", item.dataset.view === viewName));
  document.querySelectorAll(".view").forEach((view) => view.classList.toggle("active", view.id === `view-${viewName}`));
  updatePageChrome();
}

function updatePageChrome() {
  const titles = {
    live: "Сканер",
    search: "Поиск",
    pipeline: "Пайплайн",
    backtests: "Бэктесты"
  };
  document.getElementById("pageTitle").textContent = titles[selectedView] || "Щит и Меч";
  document.querySelector(".topbar").hidden = selectedView === "pipeline";
  document.querySelector(".scan-actions").hidden = selectedView !== "live";
  document.getElementById("stats").hidden = selectedView !== "live";
  document.querySelector(".workspace").classList.toggle("pipeline-mode", selectedView === "pipeline");
  document.querySelector(".workspace").classList.toggle("backtests-mode", selectedView === "backtests");
}

function researchActionFor(symbol) {
  const latest = latestResearchFor(symbol);
  if (!latest) {
    return { label: "Исследовать", title: "Запустить исследование" };
  }
  if (isResearchFresh(latest)) {
    return { label: "Открыть", title: "Открыть исследование" };
  }
  return {
    label: "Обновить",
    title: "Обновить исследование: последняя карточка старше 24ч"
  };
}

function applyResearchActionState(button, symbol) {
  const action = researchActionFor(symbol);
  button.textContent = action.label;
  button.title = action.title;
}

function latestResearchFor(symbol) {
  const normalized = String(symbol || "").toUpperCase();
  return sortResearchCards(researchCards.filter((card) => String(card.symbol || "").toUpperCase() === normalized))[0] || null;
}

function isResearchFresh(card) {
  if (!card?.created_at) return false;
  if (card.is_stale_after_24h === true) return false;
  const createdAt = Date.parse(card.created_at);
  if (!Number.isFinite(createdAt)) return false;
  return Date.now() - createdAt < 24 * 60 * 60 * 1000;
}

function sortResearchCards(cards) {
  return [...cards].sort((left, right) => {
    const leftTime = Date.parse(left.created_at || "") || 0;
    const rightTime = Date.parse(right.created_at || "") || 0;
    if (leftTime !== rightTime) return rightTime - leftTime;
    return Number(right.research_id || 0) - Number(left.research_id || 0);
  });
}

function researchCardByIdentity(symbol, runId = "", researchId = "") {
  const normalized = String(symbol || "").toUpperCase();
  const id = String(researchId || "");
  if (id) {
    const byId = researchCards.find((item) => String(item.research_id || "") === id);
    if (byId) return byId;
  }
  if (runId) {
    const byRun = researchCards.find((item) => String(item.symbol || "").toUpperCase() === normalized && item.run_id === runId);
    if (byRun) return byRun;
  }
  return latestResearchFor(normalized);
}

function bybitUrl(symbol) {
  return `https://www.bybit.com/trade/usdt/${symbol}`;
}

function emptyRow(cols) {
  return `<tr><td colspan="${cols}">Нет данных.</td></tr>`;
}

function statusLabel(status) {
  return ({
    pass: "пройдено",
    warn: "внимание",
    fail: "не прошла",
    error: "ошибка",
    skipped: "пропущено"
  })[status] || status || "пропущено";
}

function stageResultLabel(stage) {
  const metrics = stage?.metrics || {};
  if (metrics.scenario?.label) return metrics.scenario.label;
  if (metrics.fundamental_label) return metrics.fundamental_label;
  if (metrics.social_label) return metrics.social_label;
  return statusLabel(stage?.status || "skipped");
}

function stageMetricList(stage) {
  const metrics = stage?.metrics || {};
  const rows = [];
  if (metrics.fundamental_label) {
    rows.push(["Нарратив", metrics.narrative || "—"]);
    rows.push(["Тренд", metrics.trend_label || "—"]);
    rows.push(["Сила", metrics.trend_strength || "—"]);
    rows.push(["Циркуляция", ratioPct(metrics.circulating_supply_ratio) || "—"]);
  } else if (metrics.social_label) {
    rows.push(["Social Volume", wholeNumber(metrics.social_volume_current ?? metrics.social_volume_24h)]);
    rows.push(["Velocity", velocityRatio(metrics.social_volume_velocity_ratio)]);
    rows.push(["Spike", pctFromPercent(metrics.social_volume_velocity_pct)]);
    rows.push(["Окно", metrics.social_volume_timeframe || "—"]);
  }
  if (!rows.length) return "";
  return `<dl class="metric-list">${rows.map(([label, value]) => `
    <div><dt>${escapeHtml(label)}</dt><dd>${escapeHtml(value)}</dd></div>
  `).join("")}</dl>`;
}

function definitionList(rows) {
  const visibleRows = compactMetricRows(rows);
  if (!visibleRows.length) return `<p>Данных пока нет.</p>`;
  return `<dl class="metric-list">${visibleRows.map(([label, value]) => `
    <div><dt>${escapeHtml(label)}</dt><dd>${escapeHtml(value)}</dd></div>
  `).join("")}</dl>`;
}

function compactMetricRows(rows) {
  return (rows || [])
    .map(([label, value]) => [label, Array.isArray(value) ? value.join(", ") : value])
    .filter(([, value]) => {
      if (value === null || value === undefined) return false;
      const text = String(value).trim();
      return text && text !== "—" && text !== "нет данных";
    });
}

function metricNumber(value) {
  if (value === null || value === undefined || value === "") return "—";
  const number = Number(value);
  if (!Number.isFinite(number)) return "—";
  return number.toFixed(number >= 10 ? 1 : 2);
}

function wholeNumber(value) {
  if (value === null || value === undefined || value === "") return "—";
  const number = Number(value);
  if (!Number.isFinite(number)) return "—";
  return number.toLocaleString("ru-RU", { maximumFractionDigits: 0 });
}

function scoreBand(value, kind) {
  const number = Number(value || 0);
  if (kind === "risk") {
    if (number >= 65) return "высокий";
    if (number >= 35) return "средний";
    return "низкий";
  }
  if (kind === "narrative") {
    if (number >= 70) return "высокий";
    if (number >= 40) return "средний";
    return "слабый";
  }
  if (number >= 70) return "высокое";
  if (number >= 40) return "среднее";
  return "низкое";
}

function socialLevel(value) {
  if (value === null || value === undefined || value === "") return "нет данных";
  const number = Number(value);
  if (!Number.isFinite(number)) return "нет данных";
  if (number >= 70) return "высокая";
  if (number >= 40) return "средняя";
  return "низкая";
}

function velocityRatio(value) {
  if (value === null || value === undefined || value === "") return "нет истории";
  const number = Number(value);
  if (!Number.isFinite(number)) return "нет истории";
  return `${number.toFixed(2)}x`;
}

function velocityLevel(value) {
  if (value === null || value === undefined || value === "") return "нет данных";
  const number = Number(value);
  if (!Number.isFinite(number)) return "нет данных";
  if (number >= 1.75) return "высокая";
  if (number >= 1.05) return "умеренная";
  return "низкая";
}

function cvdBiasFromValue(value) {
  if (value === null || value === undefined || value === "") return "unknown";
  const number = Number(value);
  if (!Number.isFinite(number)) return "unknown";
  if (number > 0) return "positive";
  if (number < 0) return "negative";
  return "neutral";
}

function cvdMetricLabel(value, bias = cvdBiasFromValue(value)) {
  if (value === null || value === undefined || value === "" || bias === "unknown") return "CVD: нет данных";
  if (bias === "positive") return "CVD: покупатели давят";
  if (bias === "negative") return "CVD: продавцы давят";
  return "CVD: нейтрально";
}

function cvdMetricNote(value, bias = cvdBiasFromValue(value)) {
  if (value === null || value === undefined || value === "" || bias === "unknown") {
    return "CVD — баланс рыночных покупок и продаж; источник не вернул значение.";
  }
  return `Баланс рыночных покупок и продаж: ${metricNumber(value)}.`;
}

function stageLabel(stage) {
  return ({
    market_scan: "Волатильность",
    initial_selection: "Первичный отбор",
    research_charts: "Social Intelligence",
    fundamentals: "Фундаментал",
    social_filter: "Соцфильтр",
    manipulation_detector: "Манипуляторы",
    technical_analysis: "Теханализ",
    trade_plan: "Торговый план",
    final_ranking: "Резюме"
  })[stage] || stage;
}

function setupLabel(label) {
  if (!label || label === "No trade setup") return "Нет торгового сетапа";
  return label;
}

function localizedSetup(setup) {
  return {
    ...setup,
    label: setupLabel(setup?.label)
  };
}

function pct(value) {
  if (value === null || value === undefined || value === "") return "";
  return `${(Number(value) * 100).toFixed(2)}%`;
}

function money(value) {
  if (value === null || value === undefined || value === "") return "";
  const number = Number(value);
  if (number >= 1_000_000_000) return `$${(number / 1_000_000_000).toFixed(2)}B`;
  if (number >= 1_000_000) return `$${(number / 1_000_000).toFixed(1)}M`;
  if (number >= 1_000) return `$${(number / 1_000).toFixed(1)}K`;
  return `$${number.toFixed(0)}`;
}

function ratioPct(value) {
  if (value === null || value === undefined || value === "") return null;
  return `${(Number(value) * 100).toFixed(1)}%`;
}

function pctFromPercent(value) {
  if (value === null || value === undefined || value === "") return null;
  const number = Number(value);
  if (!Number.isFinite(number)) return null;
  return `${number.toFixed(1)}%`;
}

function longShort(longValue, shortValue) {
  const long = ratioPct(longValue);
  const short = ratioPct(shortValue);
  if (!long && !short) return "н/д";
  return `${long || "н/д"} / ${short || "н/д"}`;
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (char) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;"
  }[char]));
}

function formatDate(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toLocaleString("ru-RU", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit"
  });
}
