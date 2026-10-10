import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

const script = readFileSync(new URL("../static/app.js", import.meta.url), "utf8");
const functions = script.slice(0, script.lastIndexOf('document.querySelectorAll("[data-workspace]")'));

function fixture() {
  const elements = new Map();
  const element = (id) => {
    if (!elements.has(id)) elements.set(id, { hidden: true, disabled: false, value: "", style: {} });
    return elements.get(id);
  };
  const button = element("submit");
  const form = element("api-test-form");
  form.querySelector = () => button;
  element("test-api-base").value = "https://example.com";
  element("test-api-model").value = "mock";
  element("test-api-key").value = "test-credential-only";
  element("test-api-format").value = "auto";
  const requests = [];
  let analyzeError = new TypeError("Failed to fetch");
  let probeError = null;
  const probeReplies = [];
  let batch = 0;
  let challengeError = false;
  const context = vm.createContext({
    window: { DEFAULT_BANK_ID: "gpt", BANK_SUMMARIES: { gpt: {} } },
    document: { getElementById: element },
    AbortSignal,
    async fetch(url, options) {
      const body = options.body ? JSON.parse(options.body) : null;
      requests.push({ url, body });
      if (url === "/api/challenges") {
        if (challengeError) throw new TypeError("Failed to fetch");
        batch += 1;
        return { ok: true, json: async () => ({ challenges: [1, 2, 3].map(i => ({ prompt: `batch-${batch}-probe-${i}`, expected_count: 300 })) }) };
      }
      if (url === "/api/test/probe") {
        if (probeReplies.length) {
          const reply = probeReplies.shift();
          if (reply instanceof Error) throw reply;
          return { ok: !reply.error, status: reply.error ? 502 : 200, json: async () => reply };
        }
        if (probeError) return { ok: false, status: 502, json: async () => probeError };
        return { ok: true, json: async () => ({ accepted: true, text: "mock output", api_format: body.api_format === "auto" ? "openai" : body.api_format }) };
      }
      if (url === "/api/analyze") {
        if (analyzeError instanceof Error) throw analyzeError;
        if (analyzeError) return { ok: false, status: 503, json: async () => analyzeError };
        return { ok: true, json: async () => ({ used_outputs: 3 }) };
      }
      throw new Error(`Unexpected request: ${url}`);
    },
  });
  vm.runInContext(functions, context);
  vm.runInContext("renderResult = payload => { resultForTest = payload; }", context);
  return {
    context, element, requests,
    run: () => context.testViaApi({ preventDefault() {}, currentTarget: form }),
    retry: () => context.retryApiAnalysis(),
    resume: () => context.continueApiTest(),
    pending: () => vm.runInContext("state.pendingApiAnalysis", context),
    collection: () => vm.runInContext("state.pendingApiCollection", context),
    failAnalysis: (error) => { analyzeError = error; },
    failProbe: (error) => { probeError = error; },
    replyToProbes: (...replies) => { probeReplies.push(...replies); },
    failChallenges: (value) => { challengeError = value; },
  };
}

test("a disconnected attribution request keeps samples; retries never request the model again", async () => {
  const f = fixture();
  await f.run();
  assert.equal(f.requests.filter(r => r.url === "/api/test/probe").length, 3);
  assert.equal(f.pending().outputs.length, 3);
  assert.equal(f.element("retry-api-analysis").hidden, false);
  assert.equal(f.element("submit").disabled, false);
  assert.match(f.element("test-message").textContent, /三份回答已保留/);
  assert.equal(JSON.stringify(f.pending()).includes("test-credential-only"), false);
  await f.retry();
  assert.equal(f.pending().outputs.length, 3);
  f.failAnalysis(null);
  await f.retry();
  assert.equal(f.requests.filter(r => r.url === "/api/test/probe").length, 3);
  assert.equal(f.requests.filter(r => r.url === "/api/analyze").length, 3);
  assert.equal(f.context.resultForTest.api_test.received, 3);
  assert.equal(f.pending(), null);
  assert.equal(f.element("retry-api-analysis").hidden, true);
});

const gatewayFailure = { error: "Upstream gateway unavailable", upstream_status: 502, retryable: true };
const validReply = { accepted: true, text: "preserved output", api_format: "responses" };

test("a partially successful batch keeps its sample and resumes with only two new model calls", async () => {
  const f = fixture();
  f.failAnalysis(null);
  f.replyToProbes(gatewayFailure, gatewayFailure, validReply, gatewayFailure, gatewayFailure, gatewayFailure);
  await f.run();
  assert.equal(f.requests.filter(r => r.url === "/api/test/probe").length, 6);
  assert.equal(f.collection().outputs.length, 1);
  assert.equal(f.element("continue-api-test").hidden, false);
  assert.match(f.element("continue-api-test").textContent, /还缺 2 份/);
  assert.equal(JSON.stringify(f.collection()).includes("test-credential-only"), false);
  assert.equal(f.requests.some(r => r.url === "/api/analyze"), false);
  await f.resume();
  const probes = f.requests.filter(r => r.url === "/api/test/probe");
  assert.equal(probes.length, 8);
  assert.equal(probes[6].body.api_format, "responses");
  assert.equal(probes[7].body.api_format, "responses");
  assert.notEqual(probes[6].body.prompt, probes[2].body.prompt);
  assert.equal(f.context.resultForTest.api_test.received, 3);
  assert.equal(f.context.resultForTest.api_test.attempted, 8);
  const analysis = f.requests.find(r => r.url === "/api/analyze");
  assert.equal(analysis.body.outputs[0].text, "preserved output");
  assert.equal(f.collection(), null);
  assert.equal(f.element("continue-api-test").hidden, true);
});

test("three consecutive gateway failures pause before all six attempts and resume untouched challenges", async () => {
  const f = fixture();
  f.failAnalysis(null);
  f.failProbe(gatewayFailure);
  await f.run();
  assert.equal(f.requests.filter(r => r.url === "/api/test/probe").length, 3);
  assert.equal(f.collection().states.filter(s => s === "pending").length, 3);
  assert.equal(f.element("submit").disabled, false);
  assert.match(f.element("test-message").textContent, /可能消耗额度/);
  f.failProbe(null);
  await f.resume();
  assert.equal(f.requests.filter(r => r.url === "/api/challenges").length, 2);
  assert.equal(f.requests.filter(r => r.url === "/api/test/probe").length, 6);
  assert.equal(f.context.resultForTest.used_outputs, 3);
});

test("resuming refuses to mix models or temperatures, while restarting clears partial samples", async () => {
  const f = fixture();
  f.replyToProbes(validReply, gatewayFailure, gatewayFailure, gatewayFailure);
  await f.run();
  const saved = f.collection();
  f.element("test-api-model").value = "another-model";
  await f.resume();
  assert.equal(f.requests.filter(r => r.url === "/api/test/probe").length, 4);
  assert.equal(f.collection(), saved);
  assert.match(f.element("test-message").textContent, /不能混用/);
  f.element("test-api-model").value = "mock";
  f.element("test-temperature").value = "1";
  await f.resume();
  assert.equal(f.requests.filter(r => r.url === "/api/test/probe").length, 4);
  f.failProbe({ error: "Invalid credential", upstream_status: 401 });
  await f.run();
  assert.equal(f.collection().outputs.length, 0);
  assert.equal(f.requests.filter(r => r.url === "/api/test/probe").length, 5);
});

test("resumption without a credential makes no call and permits rotating the key", async () => {
  const f = fixture();
  f.failAnalysis(null);
  f.replyToProbes(validReply, { error: "Invalid credential", upstream_status: 401 });
  await f.run();
  f.element("test-api-key").value = "";
  await f.resume();
  assert.equal(f.requests.filter(r => r.url === "/api/test/probe").length, 2);
  f.element("test-api-key").value = "rotated-test-credential";
  await f.resume();
  const probes = f.requests.filter(r => r.url === "/api/test/probe");
  assert.equal(probes.length, 4);
  assert.equal(probes[2].body.api_key, "rotated-test-credential");
  assert.equal(f.context.resultForTest.used_outputs, 3);
});

test("failed challenge loading during resumption preserves the earlier successful sample", async () => {
  const f = fixture();
  f.replyToProbes(gatewayFailure, gatewayFailure, validReply, gatewayFailure, gatewayFailure, gatewayFailure);
  await f.run();
  f.failChallenges(true);
  await f.resume();
  assert.equal(f.collection().outputs.length, 1);
  assert.equal(f.element("continue-api-test").hidden, false);
  assert.equal(f.element("continue-api-test").disabled, false);
  assert.equal(f.element("submit").disabled, false);
  assert.equal(f.requests.filter(r => r.url === "/api/test/probe").length, 6);
});

test("a network error pauses collection; resumed samples still support attribution-only retry", async () => {
  const f = fixture();
  f.replyToProbes(validReply, new TypeError("Failed to fetch"), new TypeError("Failed to fetch"), new TypeError("Failed to fetch"));
  await f.run();
  assert.equal(f.collection().outputs.length, 1);
  await f.resume();
  assert.equal(f.collection(), null);
  assert.equal(f.pending().outputs.length, 3);
  assert.equal(f.element("continue-api-test").hidden, true);
  assert.equal(f.element("retry-api-analysis").hidden, false);
  assert.equal(JSON.stringify(f.pending()).includes("test-credential-only"), false);
  f.failAnalysis(null);
  await f.retry();
  assert.equal(f.requests.filter(r => r.url === "/api/test/probe").length, 6);
  assert.equal(f.pending(), null);
});

test("server errors allow attribution-only retry, while upstream authentication failure stops sampling", async () => {
  const f = fixture();
  f.failAnalysis({ error: "Service temporarily unavailable" });
  await f.run();
  assert.equal(f.pending().outputs.length, 3);
  assert.match(f.element("test-message").textContent, /Service temporarily unavailable/);
  f.failProbe({ error: "Invalid credential", upstream_status: 401 });
  await f.run();
  assert.equal(f.pending(), null);
  assert.equal(f.element("retry-api-analysis").hidden, true);
  assert.match(f.element("test-message").textContent, /Invalid credential/);
  assert.equal(f.requests.filter(r => r.url === "/api/test/probe").length, 4);
});
