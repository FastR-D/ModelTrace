const state = {
  challenges: [],
  bankId: window.DEFAULT_BANK_ID,
  bank: window.BANK_SUMMARIES[window.DEFAULT_BANK_ID],
  unified: window.UNIFIED_SUMMARY,
  pendingApiAnalysis: null,
  pendingApiCollection: null,
  apiTestRunning: false,
};

const byId = (id) => document.getElementById(id);

async function fetchJson(url, options = {}) {
  let response;
  try {
    response = await fetch(url, { ...options, signal: AbortSignal.timeout(85000) });
  } catch (cause) {
    const timedOut = ["TimeoutError", "AbortError"].includes(cause.name);
    const error = new Error(timedOut
      ? "连接本页面的服务超时，请稍后重试。"
      : "无法连接本页面的服务，请检查网络连接；本地使用时请确认服务窗口仍在运行。");
    error.retryable = true;
    throw error;
  }
  let payload;
  try { payload = await response.json(); }
  catch { throw new Error(`服务返回非 JSON 内容（HTTP ${response.status}），请稍后重试`); }
  if (!response.ok) {
    const error = new Error(payload.error || `请求失败（HTTP ${response.status}）`);
    error.httpStatus = response.status;
    error.upstreamStatus = payload.upstream_status;
    error.retryable = payload.retryable;
    throw error;
  }
  return payload;
}

function escapeHtml(value) {
  return String(value).replace(/[&<>'"]/g, (character) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;",
  })[character]);
}

function percent(value) {
  return `${(value * 100).toFixed(1)}%`;
}

function optionalNumber(id) {
  const value = byId(id).value.trim();
  return value === "" ? null : Number(value);
}

function setMessage(element, text, type = "error") {
  element.textContent = text;
  element.className = `message ${type}`;
  element.hidden = !text;
}

function activateWorkspace(name) {
  document.querySelectorAll(".workspace").forEach((item) => item.classList.toggle("active", item.id === `workspace-${name}`));
  document.querySelectorAll("[data-workspace]").forEach((item) => item.classList.toggle("active", item.dataset.workspace === name));
}

function activateMode(group, name) {
  document.querySelectorAll(`[data-${group}-mode]`).forEach((item) => item.classList.toggle("active", item.dataset[`${group}Mode`] === name));
  document.querySelectorAll(`#workspace-${group === "test" ? "test" : "library"} .mode-panel`).forEach((item) => {
    item.classList.toggle("active", item.id === `${group}-${name}` || item.id === `library-${name}`);
  });
}

async function loadChallenges() {
  byId("regenerate").disabled = true;
  byId("result").hidden = true;
  setMessage(byId("test-message"), "");
  try {
    state.challenges = (await fetchJson("/api/challenges")).challenges;
    renderChallenges();
  } catch (error) { setMessage(byId("test-message"), error.message); }
  finally { byId("regenerate").disabled = false; }
}

function renderChallenges() {
  byId("challenge-list").innerHTML = state.challenges.map((challenge, index) => `
    <article class="challenge-item">
      <div class="challenge-header">
        <strong>挑战 ${index + 1}</strong>
        <span>${challenge.expected_count} 个数字</span>
        <button type="button" data-copy="${index}">复制提示词</button>
      </div>
      <div class="challenge-columns">
        <div><label>发送给待测模型</label><pre>${escapeHtml(challenge.prompt)}</pre></div>
        <div><label for="output-${index}">粘贴完整输出</label><textarea id="output-${index}" spellcheck="false" placeholder="保留文字、标点、代码块和完整数字序列"></textarea></div>
      </div>
    </article>
  `).join("");
  document.querySelectorAll("[data-copy]").forEach((button) => {
    button.addEventListener("click", async () => {
      await navigator.clipboard.writeText(state.challenges[Number(button.dataset.copy)].prompt);
      button.textContent = "已复制";
      window.setTimeout(() => { button.textContent = "复制提示词"; }, 1000);
    });
  });
}

function renderResult(payload) {
  const diagnostics = payload.diagnostics.map((item, index) => `
    <span class="diagnostic ${item.accepted ? "accepted" : "rejected"}">挑战 ${index + 1}: ${item.parsed_numbers} 个数字 · ${item.accepted ? "计入" : "忽略"}</span>
  `).join("");
  const rows = payload.results.map((item, index) => `
    <tr class="${index === 0 ? "winner" : ""}">
      <td>${index + 1}</td><td><strong>${escapeHtml(item.display_name)}</strong></td><td>${escapeHtml(item.family_name)}</td>
      <td><div class="probability-cell"><span><i style="width:${item.probability * 100}%"></i></span><strong>${percent(item.probability)}</strong></div></td>
      <td>${percent(item.profile_similarity)}</td>
    </tr>
  `).join("");
  const apiNote = payload.api_test
    ? `<span>API 获得 ${payload.api_test.received}/${payload.api_test.requested} 份有效回答，实际尝试 ${payload.api_test.attempted}/${payload.api_test.max_attempts}${payload.api_test.errors.length ? `，${payload.api_test.errors.length} 次未采用` : ""}</span>`
    : "";
  byId("result").innerHTML = `
    <div class="result-summary">
      <div><span>最可能模型</span><strong>${escapeHtml(payload.prediction_name)}</strong></div>
      <div><span>统一库概率</span><strong>${percent(payload.probability)}</strong></div>
      <div><span>自动识别家族</span><strong>${escapeHtml(payload.family_prediction_name)} · ${percent(payload.family_probability)}</strong></div>
      <div><span>有效查询</span><strong>${payload.used_outputs}/3</strong></div>
    </div>
    <div class="diagnostics">${diagnostics}</div>
    <div class="table-wrap"><table><thead><tr><th>排序</th><th>候选模型</th><th>家族</th><th>归因概率</th><th>分布相似度</th></tr></thead><tbody>${rows}</tbody></table></div>
    ${apiNote ? `<div class="result-note">${apiNote}</div>` : ""}
    <div class="result-guidance" role="note" aria-label="结果说明">
      <p>本工具仅对指纹库内的模型进行归因；若待测模型不在指纹库中，得到任何结果都有可能。</p>
      <p>Claude Code 的系统提示词会影响模型偏好，测试结果存在较大偏差，建议不要在 Claude Code 中测试。</p>
    </div>
  `;
  byId("result").hidden = false;
  byId("result").scrollIntoView({ behavior: "smooth", block: "start" });
}

async function analyzeManual() {
  const button = byId("analyze");
  button.disabled = true;
  setMessage(byId("test-message"), "正在计算……", "working");
  const outputs = state.challenges.map((challenge, index) => ({
    text: byId(`output-${index}`).value,
    expected_count: challenge.expected_count,
  }));
  try {
    const payload = await fetchJson("/api/analyze", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ outputs }) });
    setMessage(byId("test-message"), "");
    renderResult(payload);
  } catch (error) {
    setMessage(byId("test-message"), error.message, "error");
    byId("result").hidden = true;
  }
  button.disabled = false;
}

function renderApiProgress(states, status) {
  const valid = states.filter((state) => state === "done").length;
  const attempted = states.filter((state) => ["done", "invalid", "error"].includes(state)).length;
  const target = 3;
  byId("api-test-progress").hidden = false;
  byId("api-progress-status").textContent = status;
  byId("api-progress-count").textContent = `有效 ${valid}/${target} · 已尝试 ${attempted}/${states.length}`;
  byId("api-progress-fill").style.width = `${(valid / target) * 100}%`;
  byId("api-progress-steps").innerHTML = states.map((state, index) => {
    const labels = { pending: "等待", working: "请求中", done: "有效", invalid: "数字不足", error: "接口失败", skipped: "无需调用" };
    return `<span class="progress-step ${state}"><b>${index + 1}</b>挑战 ${index + 1} · ${labels[state]}</span>`;
  }).join("");
}

async function retryApiAnalysis() {
  const pending = state.pendingApiAnalysis;
  if (!pending) return;
  const retryButton = byId("retry-api-analysis");
  const testButton = byId("api-test-form").querySelector("button[type=submit]");
  retryButton.disabled = true;
  testButton.disabled = true;
  setMessage(byId("test-message"), "");
  renderApiProgress(pending.states, "模型回答已收齐，正在计算归因概率……");
  try {
    const result = await fetchJson("/api/analyze", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ outputs: pending.outputs }),
    });
    const attempted = pending.states.filter((item) => ["done", "invalid", "error"].includes(item)).length;
    result.api_test = {
      requested: 3, attempted, max_attempts: pending.states.length,
      received: pending.outputs.length, errors: pending.errors,
    };
    renderResult(result);
    renderApiProgress(pending.states, `测试完成：${pending.outputs.length}/3 份有效回答进入归因`);
    state.pendingApiAnalysis = null;
    retryButton.hidden = true;
  } catch (error) {
    renderApiProgress(pending.states, "归因计算未完成，已保留本次回答，可重试归因");
    setMessage(byId("test-message"), `${error.message} 三份回答已保留在当前页面内存中，点击“重试归因”即可，不会再次调用模型接口；请暂时不要刷新页面。`, "error");
    retryButton.hidden = false;
  } finally {
    retryButton.disabled = false;
    testButton.disabled = false;
  }
}

function apiTestConfiguration() {
  return {
    base_url: byId("test-api-base").value.trim().replace(/\/+$/, ""),
    api_model: byId("test-api-model").value.trim(),
    temperature: optionalNumber("test-temperature"),
    api_format: byId("test-api-format").value,
  };
}

function showPendingCollection(pending) {
  const remaining = 3 - pending.outputs.length;
  const button = byId("continue-api-test");
  button.hidden = false;
  button.textContent = `继续采集（还缺 ${remaining} 份）`;
  byId("api-test-form").querySelector("button[type=submit]").textContent = "开始新测试";
  renderApiProgress(pending.states, `采集暂停：已保留 ${pending.outputs.length}/3 份有效回答`);
  setMessage(byId("test-message"), `${pending.errors.at(-1) || "未收齐三份有效回答。"} 已保留 ${pending.outputs.length} 份回答，可稍后继续采集；继续会调用模型接口并可能消耗额度，请不要刷新页面。`, "error");
}

async function collectApiAnswers(pending) {
  const { challenges, states, outputs, errors } = pending;
  const configuration = { ...pending.configuration, api_key: byId("test-api-key").value.trim(), api_format: pending.apiFormat };
  let consecutiveFailures = 0;

  for (let index = 0; index < challenges.length && outputs.length < 3; index += 1) {
    if (states[index] !== "pending") continue;
    states[index] = "working";
    renderApiProgress(states, `正在进行第 ${index + 1} 次尝试，等待模型完整输出……`);
    try {
      const payload = await fetchJson("/api/test/probe", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          ...configuration,
          prompt: challenges[index].prompt,
          expected_count: challenges[index].expected_count,
        }),
      });
      consecutiveFailures = 0;
      if (payload.api_format) {
        configuration.api_format = payload.api_format;
        pending.apiFormat = payload.api_format;
      }
      if (payload.accepted) {
        outputs.push({ text: payload.text, expected_count: challenges[index].expected_count });
        states[index] = "done";
      } else {
        errors.push(`尝试 ${index + 1}: 有效数字 ${payload.parsed_numbers}/${payload.minimum_numbers}`);
        states[index] = "invalid";
      }
    } catch (error) {
      errors.push(`尝试 ${index + 1}: ${error.message}`);
      states[index] = "error";
      consecutiveFailures += 1;
      if ([400, 401, 403, 404, 405, 415, 429].includes(error.upstreamStatus) || [400, 403, 413, 415].includes(error.httpStatus)) break;
      if (consecutiveFailures >= 3) break;
    }
    renderApiProgress(states, `当前已有 ${outputs.length}/3 份有效回答`);
  }

  if (outputs.length < 3) {
    showPendingCollection(pending);
    return;
  }

  states.forEach((item, index) => { if (item === "pending") states[index] = "skipped"; });
  state.pendingApiCollection = null;
  byId("continue-api-test").hidden = true;
  byId("api-test-form").querySelector("button[type=submit]").textContent = "开始测试";
  state.pendingApiAnalysis = { outputs, states, errors };
  await retryApiAnalysis();
}

async function continueApiTest() {
  const pending = state.pendingApiCollection;
  if (!pending || state.apiTestRunning) return;
  if (JSON.stringify(apiTestConfiguration()) !== JSON.stringify(pending.configuration)) {
    setMessage(byId("test-message"), "接口、模型、温度或协议已改变，不能混用之前的回答。恢复原设置后继续采集，或点击“开始新测试”。", "error");
    return;
  }
  if (!byId("test-api-key").value.trim()) {
    setMessage(byId("test-message"), "请填写 API Key 后继续采集。", "error");
    return;
  }
  const button = byId("api-test-form").querySelector("button[type=submit]");
  state.apiTestRunning = true;
  button.disabled = true;
  byId("continue-api-test").disabled = true;
  setMessage(byId("test-message"), "");
  try {
    if (!pending.states.includes("pending")) {
      const nextBatch = (await fetchJson("/api/challenges")).challenges;
      pending.challenges.push(...nextBatch);
      pending.states.push(...nextBatch.map(() => "pending"));
    }
    await collectApiAnswers(pending);
  } catch (error) {
    pending.errors.push(error.message);
    showPendingCollection(pending);
  } finally {
    state.apiTestRunning = false;
    button.disabled = false;
    byId("continue-api-test").disabled = false;
  }
}

async function testViaApi(event) {
  event.preventDefault();
  if (state.apiTestRunning) return;
  const button = event.currentTarget.querySelector("button[type=submit]");
  state.apiTestRunning = true;
  button.disabled = true;
  button.textContent = "开始测试";
  byId("result").hidden = true;
  setMessage(byId("test-message"), "");
  state.pendingApiAnalysis = null;
  state.pendingApiCollection = null;
  byId("retry-api-analysis").hidden = true;
  byId("continue-api-test").hidden = true;
  try {
    const configuration = apiTestConfiguration();
    const firstBatch = (await fetchJson("/api/challenges")).challenges;
    const challenges = firstBatch.concat((await fetchJson("/api/challenges")).challenges);
    const pending = { challenges, states: challenges.map(() => "pending"), outputs: [], errors: [], configuration, apiFormat: configuration.api_format };
    state.pendingApiCollection = pending;
    renderApiProgress(pending.states, "已生成独立挑战，准备调用模型");
    await collectApiAnswers(pending);
  } catch (error) { setMessage(byId("test-message"), error.message, "error"); }
  finally { state.apiTestRunning = false; button.disabled = false; }
}

function updateUnifiedSummary(summary) {
  state.unified = summary;
  byId("topbar-bank-count").textContent = `${summary.model_count} 个候选模型`;
  byId("active-bank-badge").textContent = `${summary.model_count} 个候选模型`;
}

function renderInventory() {
  byId("selected-bank-name").textContent = state.bank.label;
  byId("model-options").innerHTML = state.bank.models.map((model) => `<option value="${escapeHtml(model.id)}"></option>`).join("");
  byId("bank-inventory").innerHTML = state.bank.models.length
    ? state.bank.models.map((model) => `<span class="fingerprint-item">${escapeHtml(model.display_name)}</span>`).join("")
    : `<span class="empty-inventory">暂无指纹</span>`;
}

async function refreshBank() {
  const response = await fetch(`/api/bank?bank_id=${encodeURIComponent(state.bankId)}`);
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || "无法读取指纹库");
  state.bank = payload;
  renderInventory();
}

async function selectBank(bankId) {
  state.bankId = bankId;
  await refreshBank();
}

async function enrollAutomatically(event) {
  event.preventDefault();
  const button = event.currentTarget.querySelector("button[type=submit]");
  button.disabled = true;
  const requested = Number(byId("sample-count").value);
  const started = Date.now();
  const progressTimer = window.setInterval(() => {
    const seconds = Math.floor((Date.now() - started) / 1000);
    setMessage(byId("enrollment-message"), `正在自动识别协议并采集 ${requested} 份回答 · 已等待 ${seconds} 秒`, "working");
  }, 1000);
  setMessage(byId("enrollment-message"), `正在自动识别协议并采集 ${requested} 份回答`, "working");
  let response;
  try {
    response = await fetch("/api/enroll/auto", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        base_url: byId("api-base").value,
        api_key: byId("api-key").value,
        api_model: byId("api-model").value,
        bank_id: state.bankId,
        model_label: byId("auto-model").value,
        sample_count: requested,
        temperature: optionalNumber("temperature"),
      }),
    });
  } catch (error) {
    window.clearInterval(progressTimer);
    setMessage(byId("enrollment-message"), error.message, "error");
    button.disabled = false;
    return;
  }
  window.clearInterval(progressTimer);
  const payload = await response.json();
  if (response.ok) {
    state.bank = payload.bank;
    updateUnifiedSummary(payload.unified);
    renderInventory();
    setMessage(byId("enrollment-message"), `采集完成：收到 ${payload.received}/${payload.requested} 份，${payload.accepted} 份进入指纹库，${payload.rejected} 份无效，${payload.errors.length} 次接口错误。`, "success");
  } else {
    setMessage(byId("enrollment-message"), payload.error || "自动采集失败。", "error");
  }
  button.disabled = false;
}

function renderBankOptions(summaries, selected) {
  byId("bank-select").innerHTML = Object.entries(summaries)
    .map(([bankId, bank]) => `<option value="${escapeHtml(bankId)}"${bankId === selected ? " selected" : ""}>${escapeHtml(bank.label)}</option>`)
    .join("");
}

async function createBank(event) {
  event.preventDefault();
  const button = event.currentTarget.querySelector("button[type=submit]");
  button.disabled = true;
  const response = await fetch("/api/banks", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ label: byId("new-bank-name").value }),
  });
  const payload = await response.json();
  if (response.ok) {
    window.BANK_SUMMARIES = payload.banks;
    state.bankId = payload.bank.id;
    state.bank = payload.bank;
    updateUnifiedSummary(payload.unified);
    renderBankOptions(payload.banks, state.bankId);
    renderInventory();
    byId("new-bank-name").value = "";
    byId("create-bank-form").hidden = true;
    setMessage(byId("enrollment-message"), `已创建 ${payload.bank.label}`, "success");
  } else {
    setMessage(byId("enrollment-message"), payload.error || "创建失败。", "error");
  }
  button.disabled = false;
}

document.querySelectorAll("[data-workspace]").forEach((button) => button.addEventListener("click", () => activateWorkspace(button.dataset.workspace)));
document.querySelectorAll("[data-test-mode]").forEach((button) => button.addEventListener("click", () => activateMode("test", button.dataset.testMode)));
byId("bank-select").addEventListener("change", (event) => selectBank(event.target.value));
byId("regenerate").addEventListener("click", loadChallenges);
byId("analyze").addEventListener("click", analyzeManual);
byId("api-test-form").addEventListener("submit", testViaApi);
byId("retry-api-analysis").addEventListener("click", retryApiAnalysis);
byId("continue-api-test").addEventListener("click", continueApiTest);
byId("auto-enrollment")?.addEventListener("submit", enrollAutomatically);
byId("show-create-bank")?.addEventListener("click", () => { byId("create-bank-form").hidden = !byId("create-bank-form").hidden; });
byId("create-bank-form")?.addEventListener("submit", createBank);

renderInventory();
loadChallenges();
