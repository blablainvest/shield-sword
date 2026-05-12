let currentReport = null;
let researchCards = [];
let selectedView = "live";
const sortState = {
  topLong: { key: "price_change_pct", direction: "desc" },
  topShort: { key: "price_change_pct", direction: "asc" }
};

const stageOrder = [
  "market_scan",
  "initial_selection",
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
  return `<article class="pipeline-card" data-symbol="${card.symbol}" data-run-id="${card.run_id || ""}" data-research-id="${card.research_id || ""}">
    <div class="pipeline-header">
      <div>
        <time>${formatDate(card.created_at)}</time>
        <strong>${card.symbol}</strong>
      </div>
    </div>
    <div class="pipeline-badges">${pipelineBadgeList(stages)}</div>
  </article>`;
}

function openResearchCard(symbol, runId = "", researchId = "") {
  const card = researchCardByIdentity(symbol, runId, researchId);
  if (!card) return;
  const setup = card.setup || {};
  const pipeline = card.pipeline || {};
  const stages = stageOrder.map((name) => (pipeline.stages || []).find((stage) => stage.stage === name)).filter(Boolean);
  const blockingStage = stages.find((stage) => stage?.blocking || stage?.status === "fail" || stage?.status === "error");
  document.getElementById("drawerContent").innerHTML = `
    <h1>${card.symbol}</h1>
    <p>${setupLabel(setup.label)} | ${escapeHtml(card.strategy_identifier || pipeline.candidate?.strategy_identifier || "unknown")} | ${escapeHtml(setup.reason || "")}</p>
    <h2>Резюме</h2>
    ${bulletList(card.summary)}
    <h2>Почему движение</h2>
    ${bulletList(card.why_it_moved)}
    <h2>Ссылки</h2>
    <div class="link-list">
      ${Object.entries(card.links || {}).map(([label, url]) => `<a href="${url}" target="_blank" rel="noreferrer">${label}</a>`).join("")}
    </div>
    <h2>Фундаментал</h2>
    ${stageSummary(card.fundamentals, blockingStage)}
    <h2>Соцфильтр</h2>
    ${stageSummary(card.sentiment, blockingStage)}
    <h2>Риск ликвидности / манипулятивности</h2>
    ${stageSummary(card.manipulation, blockingStage)}
    <h2>Теханализ</h2>
    ${technicalAnalysisSummary(card.technical_analysis, card.strategy_identifier || pipeline.candidate?.strategy_identifier)}
    <h2>Сетап</h2>
    <pre>${escapeHtml(JSON.stringify(localizedSetup(setup), null, 2))}</pre>
    <h2>Пайплайн</h2>
    <div class="timeline">${stages.map(stageBlock).join("")}</div>
  `;
  document.getElementById("drawer").classList.add("open");
}

function stageSummary(stage, blockingStage = null) {
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

function socialSummary(stage) {
  const metrics = stage?.metrics || {};
  const volumeRows = [
    ["Social Volume", wholeNumber(metrics.social_volume_current ?? metrics.social_volume_24h)],
    ["Baseline", wholeNumber(metrics.social_volume_baseline)],
    ["Previous", wholeNumber(metrics.social_volume_previous)],
    ["Velocity", velocityRatio(metrics.social_volume_velocity_ratio)],
    ["Spike", pctFromPercent(metrics.social_volume_velocity_pct)],
    ["Окно", metrics.social_volume_timeframe],
    ["Источник", metrics.social_volume_source || metrics.data_coverage]
  ];
  const contextRows = [
    ["Темы", metrics.topics],
    ["Why moved", metrics.why_moved],
    ["Alerts", metrics.alerts || metrics.supportive_causes],
  ];
  return `<div class="stage-summary social-summary">
    <span class="badge ${stage?.status || "skipped"}">${stageResultLabel(stage)}</span>
    <p>${escapeHtml(stageReason(stage) || "Этап еще не запускался.")}</p>
    <section>
      <h3>Social Volume Velocity</h3>
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
  const signals = metrics.signals || {};
  const signalRows = Object.entries(signals).map(([name, signal]) => [
    name,
    `${signal?.status || "нет данных"}: ${signal?.value === null || signal?.value === undefined ? "—" : signal.value}`
  ]);
  const onchain = metrics.onchain_filter?.metrics || {};
  const derivativeRows = [
    ["Status", metrics.onchain_filter?.status || "unavailable"],
    ["Funding", pct(onchain.funding_rate) || "—"],
    ["Open Interest", money(onchain.open_interest_value) || metricNumber(onchain.open_interest)],
    ["Long / Short", longShort(onchain.long_ratio, onchain.short_ratio)],
    ["L/S source", onchain.long_short_ratio_status || "unavailable"],
    ["CVD", onchain.cvd?.status || "unavailable"],
    ["Liquidation clusters", onchain.liquidation_clusters?.status || "unavailable"],
    ["OI / Market Cap", onchain.oi_market_cap_ratio?.status || "unavailable"]
  ];
  const execution = metrics.execution_context || {};
  return `<div class="stage-summary technical-summary">
    <span class="badge ${stage.status || "skipped"}">${stageResultLabel(stage)}</span>
    <p>${escapeHtml(metrics.principle || "Onchain/derivatives определяют что торговать; ТА определяет когда и где входить.")}</p>
    <section>
      <h3>Идентификатор стратегии</h3>
      <p><strong>${escapeHtml(metrics.strategy_identifier || metrics.strategy_models?.selected || fallbackStrategy || "unknown")}</strong></p>
    </section>
    <section>
      <h3>Onchain / derivatives filter</h3>
      ${definitionList(derivativeRows)}
    </section>
    <section>
      <h3>TA signals</h3>
      ${definitionList(signalRows.length ? signalRows : [["status", "insufficient_data"]])}
    </section>
    <section>
      <h3>Исполнение</h3>
      ${definitionList([
        ["Вход", execution.entry_basis || "TA confirmation"],
        ["Stop-loss", execution.stop_loss_basis || "ATR / structure"],
        ["Take-profit", execution.take_profit_basis || "structure / liquidity"]
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
  const githubRepos = Array.isArray(metrics.github_repos) ? metrics.github_repos.join(", ") : metrics.github_repos;
  const identityRows = [
    ["Название", metrics.name],
    ["Категория", metrics.sector],
    ["Chain / ecosystem", metrics.chain_ecosystem],
    ["Contract", metrics.contract_address],
    ["Homepage", metrics.homepage_url],
    ["Whitepaper", metrics.whitepaper_url],
    ["X / Twitter", metrics.twitter_screen_name ? `@${metrics.twitter_screen_name}` : ""],
    ["Telegram", metrics.telegram_channel_identifier],
    ["Subreddit", metrics.subreddit_url],
    ["GitHub", githubRepos],
  ];
  const sizeRows = [
    ["FDV tier", metrics.fdv_tier_label || metrics.market_cap_tier_label || "FDV: нет данных"],
    ["FDV", money(metrics.fdv) || "—"],
    ["Market cap", money(metrics.market_cap) || "—"],
    ["MC / FDV", ratioPct(metrics.market_cap_to_fdv_ratio) || "—"],
    ["Supply profile", metrics.market_cap_to_fdv_label],
    ["Объем / Market cap", ratioPct(metrics.volume_to_market_cap) || "—"],
    ["Цена 24ч", pctFromPercent(metrics.price_change_24h)],
    ["Цена 7д", pctFromPercent(metrics.price_change_7d)]
  ];
  return `<div class="stage-summary fundamental-summary">
    <span class="badge ${stage?.status || "skipped"}">${stageResultLabel(stage)}</span>
    <div class="fundamental-card">
      <section>
        <h3>Кто перед нами</h3>
        <p>${escapeHtml(metrics.project_brief_ru || metrics.project_summary || "Данных пока нет.")}</p>
        ${definitionList(identityRows)}
      </section>
      <section>
        <h3>Размер и supply</h3>
        ${definitionList(sizeRows)}
        ${metrics.fdv_tier_reason ? `<p>${escapeHtml(metrics.fdv_tier_reason)}</p>` : ""}
        ${metrics.market_cap_to_fdv_reason ? `<p>${escapeHtml(metrics.market_cap_to_fdv_reason)}</p>` : ""}
      </section>
      <section>
        <h3>Почему могло двигаться</h3>
        <p><strong>${escapeHtml(metrics.fundamental_label || "Недостаточно данных")}</strong></p>
        ${metrics.fundamental_label_reason ? `<p>${escapeHtml(metrics.fundamental_label_reason)}</p>` : ""}
        ${bulletList(cleanList(metrics.movement_type_reasons).length ? metrics.movement_type_reasons : metrics.movement_supportive_ru)}
      </section>
    </div>
  </div>`;
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

function stageLabel(stage) {
  return ({
    market_scan: "Волатильность",
    initial_selection: "Первичный отбор",
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
