const { chromium } = require("playwright");
const fs = require("fs");
const path = require("path");
const { pathToFileURL } = require("url");

let browser;
(async () => {
  const launchOptions = { headless: true };
  const requestedBrowser = process.env.BROWSER_EXECUTABLE_PATH;
  if (requestedBrowser) {
    if (!fs.existsSync(requestedBrowser)) throw new Error(`BROWSER_EXECUTABLE_PATH does not exist: ${requestedBrowser}`);
    launchOptions.executablePath = requestedBrowser;
  }
  browser = await chromium.launch(launchOptions);
  const page = await browser.newPage({ viewport: { width: 1440, height: 1050 }, deviceScaleFactor: 1 });
  const errors = [];
  page.on("console", (message) => {
    const expectedDemoFallback = process.env.PUBLIC_DEMO_ONLY === "1"
      && message.text().includes("Failed to load resource: net::ERR_FAILED");
    if (message.type() === "error" && !expectedDemoFallback) errors.push(`console: ${message.text()}`);
  });
  page.on("pageerror", (error) => errors.push(`pageerror: ${error.message}`));
  if (process.env.PUBLIC_DEMO_ONLY === "1") {
    await page.route("**/data.js*", (route) => route.abort());
  }
  const url = pathToFileURL(path.resolve(process.cwd(), "dashboard.html")).href;
  await page.goto(url, { waitUntil: "load" });
  await page.waitForSelector(".sector-card[data-sector-key]");

  if (process.env.QA_PREVIEW_PATH) {
    const previewPath = path.resolve(process.env.QA_PREVIEW_PATH);
    fs.mkdirSync(path.dirname(previewPath), { recursive: true });
    await page.screenshot({ path: previewPath, type: "jpeg", quality: 84, fullPage: false });
  }

  const summary = await page.evaluate(() => ({
    title: document.title,
    sectorCards: document.querySelectorAll(".sector-card[data-sector-key]").length,
    parentButtons: document.querySelectorAll("[data-sector-parent]").length,
    unavailableCards: document.querySelectorAll(".sector-card.is-unavailable").length,
    hasCpo: [...document.querySelectorAll(".sector-title h3")].some((node) => node.textContent.includes("CPO/光模块")),
    hasPcb: [...document.querySelectorAll(".sector-title h3")].some((node) => node.textContent.trim() === "PCB"),
    hasAiCompute: [...document.querySelectorAll(".sector-title h3")].some((node) => node.textContent.trim() === "AI算力"),
  }));
  if (summary.sectorCards !== 30) throw new Error(`expected 30 sector cards, got ${summary.sectorCards}`);
  if (summary.parentButtons !== 9) throw new Error(`expected 9 parent filter buttons, got ${summary.parentButtons}`);
  if (!summary.hasCpo || !summary.hasPcb || !summary.hasAiCompute) throw new Error("missing required fine sectors");

  await page.screenshot({ path: path.resolve("data", "qa_sector_catalog.png"), fullPage: true });
  await page.locator('[data-sector-key="optical_interconnect"] [data-sector-open]').click();
  await page.waitForSelector("#sector-dialog[open]");
  const detail = await page.evaluate(() => ({
    title: document.querySelector("#sector-dialog-title")?.textContent || "",
    companies: document.querySelectorAll(".sector-detail-companies article").length,
    summary: document.querySelector("#sector-dialog-summary")?.textContent || "",
    fairnessPanel: Boolean(document.querySelector(".sector-fairness")),
    fairnessCompanyCards: document.querySelectorAll(".fairness-company-grid article").length,
    fairnessDayCards: document.querySelectorAll(".fairness-day-grid article").length,
    historyBreakNotice: document.querySelector(".history-method-break")?.textContent || "",
  }));
  if (!detail.title.includes("CPO/光模块")) throw new Error(`wrong detail title: ${detail.title}`);
  if (detail.companies !== 3) throw new Error(`expected 3 representative companies, got ${detail.companies}`);
  if (!detail.summary.includes("高速光模块")) throw new Error("CPO boundary description not rendered");
  if (!detail.fairnessPanel || detail.fairnessCompanyCards !== 3 || detail.fairnessDayCards !== 3) {
    throw new Error(`fairness panel incomplete: ${JSON.stringify(detail)}`);
  }
  if (!detail.historyBreakNotice.includes("断点两侧")) throw new Error("methodology break notice missing");
  await page.screenshot({ path: path.resolve("data", "qa_sector_detail.png"), fullPage: true });

  await page.locator("[data-sector-close]").click();
  await page.locator('[data-sector-parent="technology"]').click();
  const technologyCards = await page.locator(".sector-card[data-sector-key]").count();
  if (technologyCards !== 8) throw new Error(`expected 8 technology cards, got ${technologyCards}`);
  if (errors.length) throw new Error(errors.join("\n"));

  process.stdout.write(JSON.stringify({ summary, detail, technologyCards }, null, 2));
})().catch((error) => {
  process.stderr.write(`${error.stack || error}\n`);
  process.exitCode = 1;
}).finally(async () => {
  if (browser) await browser.close().catch(() => {});
});
