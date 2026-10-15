import assert from "node:assert/strict"
import { createRequire } from "node:module"
import { mkdtemp, mkdir, readFile, writeFile } from "node:fs/promises"
import path from "node:path"
import { fileURLToPath } from "node:url"
import test from "node:test"
import { chromium } from "@playwright/test"
import react from "@vitejs/plugin-react"
import { createServer } from "vite"

const frontend = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../../..")
const workspace = path.dirname(frontend)
const require = createRequire(import.meta.url)
const tailwindConfig = require("tailwindcss/loadConfig")(path.join(frontend, "tailwind.config.ts"))

// Real, deterministic three-page PDF: exercise PDF.js parsing, worker rendering,
// text indexing and original document colors, rather than mocking browser layout.
function fixturePdf() {
  const objects = [
    "<< /Type /Catalog /Pages 2 0 R >>",
    "<< /Type /Pages /Kids [4 0 R 6 0 R 8 0 R] /Count 3 >>",
    "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
  ]
  for (let page = 1; page <= 3; page += 1) {
    const contentId = objects.length + 2
    const stream = `1 0 0 rg 10 10 80 40 re f\n0 0 0 rg BT /F1 18 Tf 50 700 Td (Needle evidence page ${page}) Tj ET\n`
    objects.push(`<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 3 0 R >> >> /Contents ${contentId} 0 R >>`)
    objects.push(`<< /Length ${Buffer.byteLength(stream)} >>\nstream\n${stream}endstream`)
  }
  let pdf = "%PDF-1.4\n"
  const offsets = [0]
  objects.forEach((object, index) => {
    offsets.push(Buffer.byteLength(pdf))
    pdf += `${index + 1} 0 obj\n${object}\nendobj\n`
  })
  const xref = Buffer.byteLength(pdf)
  pdf += `xref\n0 ${offsets.length}\n0000000000 65535 f \n${offsets.slice(1).map(offset => `${String(offset).padStart(10, "0")} 00000 n \n`).join("")}trailer\n<< /Size ${offsets.length} /Root 1 0 R >>\nstartxref\n${xref}\n%%EOF\n`
  return Buffer.from(pdf)
}

// Standalone browser component regression, not application/business E2E.
// Run from frontend: node src/components/papers/__tests__/paper-pdf-viewer.layout.browser.mjs
// It owns only an ephemeral loopback Vite server, never K's app/API ports.
// CSS zoom is a layout stressor here, not a claim of native Electron zoom coverage.
test("paper-agent PDF chrome fits its reader column without clipping controls or recoloring the PDF", { timeout: 180_000 }, async () => {
  const temporaryRoot = path.join(workspace, ".tmp", "pdf-layout-browser")
  await mkdir(temporaryRoot, { recursive: true })
  const fixtureRoot = await mkdtemp(path.join(temporaryRoot, "run-"))
  const viewerFile = path.join(frontend, "src/components/papers/shared/paper-pdf-viewer.tsx").replaceAll("\\", "/")
  const stylesFile = path.join(frontend, "src/features/paper-agent-app/paper-agent-app.module.css").replaceAll("\\", "/")
  await writeFile(path.join(fixtureRoot, "index.html"), '<html><body><div id="root"></div><script type="module" src="/entry.tsx"></script></body></html>')
  await writeFile(path.join(fixtureRoot, "entry.tsx"), `
    import React from "react"
    import { createRoot } from "react-dom/client"
    import { PaperPdfViewer } from ${JSON.stringify(viewerFile)}
    import styles from ${JSON.stringify(stylesFile)}
    import "./fixture.css"
    const query = new URLSearchParams(location.search)
    const agent = query.get("appearance") !== "default"
    const title = "Evidence-" + "long-unbroken-source-title-".repeat(7) + ".pdf"
    const notes = [1, 2].map(pageNumber => ({
      noteId: "fixture-note-" + pageNumber, userId: "fixture-user", paperId: "fixture-paper",
      kind: "highlight" as const, pageNumber, color: "green" as const, quote: "Fixture anchored highlight",
      anchor: { pageNumber, quote: "Fixture anchored highlight", rects: [{ left: 12, top: 14, width: 28, height: 3 }] },
      createdAt: "2026-10-15T00:00:00Z", updatedAt: "2026-10-15T00:00:00Z",
    }))
    createRoot(document.getElementById("root")!).render(
      <div id="fixture-shell" className={styles.app + " fixture-app"} style={{ zoom: Number(query.get("zoom") || 1) }} data-theme={query.get("theme") || "light"}>
        <div className="fixture-reader" style={{ width: Number(query.get("width") || 360) }}>
          <PaperPdfViewer title={title} notes={notes} locale={query.get("locale") === "zh" ? "zh" : "en"} pdfUrl="https://example.test/layout-fixture.pdf" appearance={agent ? "paper-agent" : "default"} className={styles.pdfViewer} fallback={<p>Unexpected PDF failure</p>} />
        </div>
      </div>
    )
  `)
  await writeFile(path.join(fixtureRoot, "fixture.css"), `@tailwind base; @tailwind components; @tailwind utilities;
    body { margin: 0; }
    .fixture-app { display: block !important; min-height: 0; width: 100%; }
    .fixture-reader { display: grid; grid-template-columns: minmax(0, 1fr); max-width: 100%; min-width: 0; }
  `)
  const pdf = fixturePdf()
  const server = await createServer({
    configFile: false,
    root: fixtureRoot,
    cacheDir: path.join(fixtureRoot, "vite-cache"),
    plugins: [
      {
        name: "fixture-pdf-transport",
        configureServer(server) {
          server.middlewares.use(async (request, response, next) => {
            if (request.url?.startsWith("/api/papers/pdf")) {
              response.setHeader("Content-Type", "application/pdf")
              response.end(pdf)
            } else if (request.url === "/pdf.worker.min.mjs") {
              response.setHeader("Content-Type", "text/javascript")
              response.end(await readFile(path.join(frontend, "node_modules/pdfjs-dist/build/pdf.worker.min.mjs")))
            } else {
              next()
            }
          })
        },
      },
      react(),
    ],
    resolve: {
      alias: {
        "@": path.join(frontend, "src"),
        "react-dom": path.join(frontend, "node_modules/react-dom"),
        react: path.join(frontend, "node_modules/react"),
      },
    },
    css: {
      postcss: {
        plugins: [
          require("tailwindcss")({ ...tailwindConfig, content: [path.join(frontend, "src/**/*.{ts,tsx}").replaceAll("\\", "/")] }),
          require("autoprefixer")(),
        ],
      },
    },
    server: { host: "127.0.0.1", port: 0, strictPort: true, fs: { allow: [workspace] } },
  })
  let browser
  let page
  const evidence = {
    scope: "real component + real fixture PDF + scoped production CSS; isolated browser layout with CSS zoom stress, not business E2E or native Electron zoom",
    cases: [],
    errors: [],
  }
  try {
    await server.listen()
    const address = server.httpServer.address()
    assert.ok(address && typeof address !== "string")
    browser = await chromium.launch({ headless: true })
    page = await browser.newPage({ viewport: { width: 1440, height: 1000 } })
    page.setDefaultTimeout(15_000)
    page.setDefaultNavigationTimeout(15_000)
    page.on("pageerror", error => evidence.errors.push(error.message))

    const geometry = () => page.evaluate(() => {
      const viewer = document.querySelector("[data-pdf-appearance]")
      const boundary = viewer.getBoundingClientRect()
      function rect(element) {
        const value = element.getBoundingClientRect()
        return { left: value.left, right: value.right, top: value.top, bottom: value.bottom, width: value.width, height: value.height }
      }
      return {
        viewer: rect(viewer),
        chrome: [...viewer.querySelectorAll("[data-pdf-toolbar], [data-pdf-search]")].map(element => ({
          slot: element.hasAttribute("data-pdf-toolbar") ? "toolbar" : "search",
          rect: rect(element), clientWidth: element.clientWidth, scrollWidth: element.scrollWidth,
        })),
        searchInputWidth: viewer.querySelector("[data-pdf-search-field] input").getBoundingClientRect().width,
        thumbnailsVisible: getComputedStyle(viewer.querySelector("[data-pdf-thumbnails]")).display !== "none",
        controls: [...viewer.querySelectorAll("[data-pdf-toolbar] button, [data-pdf-toolbar] input, [data-pdf-search] button, [data-pdf-search] input, [data-pdf-search-status], [data-pdf-zoom-value], [data-pdf-page-form] span")].map(element => {
          const value = element.getBoundingClientRect()
          const clippedBy = []
          for (let ancestor = element.parentElement; ancestor; ancestor = ancestor.parentElement) {
            const style = getComputedStyle(ancestor)
            const box = ancestor.getBoundingClientRect()
            if ((style.overflowX === "clip" || style.overflowX === "hidden") && (value.left < box.left - 1 || value.right > box.right + 1)) {
              clippedBy.push(ancestor.tagName)
            }
          }
          return {
            label: element.getAttribute("aria-label") || element.textContent,
            ...rect(element),
            inColumn: value.left >= boundary.left - 1 && value.right <= boundary.right + 1,
            clippedBy,
            contentClipped: element.tagName !== "INPUT" && element.scrollWidth > element.clientWidth + 1,
          }
        }),
      }
    })
    const assertFits = measurements => {
      assert.ok(measurements.controls.length >= 11, "must measure actual pagination, zoom, search controls and their status text")
      assert.deepEqual(measurements.controls.filter(control => !control.inColumn || control.width < 1 || control.height < 1 || control.clippedBy.length || control.contentClipped), [], "every essential PDF chrome control must fit the reader, even when an outer ancestor hides overflow")
      assert.ok(measurements.searchInputWidth >= Math.min(160, measurements.viewer.width - 32), "PDF search input must have a readable width instead of being squeezed by status/navigation")
      for (let index = 0; index < measurements.controls.length; index += 1) {
        const current = measurements.controls[index]
        for (const other of measurements.controls.slice(index + 1)) {
          const overlapWidth = Math.min(current.right, other.right) - Math.max(current.left, other.left)
          const overlapHeight = Math.min(current.bottom, other.bottom) - Math.max(current.top, other.top)
          assert.ok(overlapWidth <= 1 || overlapHeight <= 1, `${current.label} must not overlap ${other.label}`)
        }
      }
      for (const chrome of measurements.chrome) {
        assert.ok(chrome.scrollWidth <= chrome.clientWidth + 1, `${chrome.slot} must wrap, not hide its overflowing children`)
      }
    }
    const waitForRenderedPage = (pageNumber, percent = 100) => page.waitForFunction(({ pageNumber, percent }) => {
      const canvas = document.querySelector("[data-pdf-document] canvas")
      const text = document.querySelector("[data-pdf-text-layer]")
      // A page-count label or an old text layer alone cannot acknowledge rendering.
      // Wait for the current-size canvas to contain the real fixture's red pixels.
      const width = Math.floor(595 * percent / 100)
      if (!canvas || !text || Number.parseFloat(canvas.style.width) !== width
        || !text.textContent?.includes(`Needle evidence page ${pageNumber}`)
        || document.querySelector("[data-pdf-canvas-scroll] [data-pdf-message]")) return false
      const pixel = canvas.getContext("2d").getImageData(Math.floor(canvas.width * 50 / 595), Math.floor(canvas.height * 812 / 842), 1, 1).data
      return pixel[0] > 200 && pixel[1] < 30 && pixel[2] < 30
    }, { pageNumber, percent })
    const cases = [
      { width: 240, zoom: 1.25, theme: "light", locale: "en" },
      { width: 280, zoom: 1, theme: "light", locale: "en" },
      { width: 360, zoom: 1.25, theme: "dark", locale: "en" },
      { width: 400, zoom: 1.25, theme: "dark", locale: "zh" },
      { width: 520, zoom: 1.25, theme: "light", locale: "en" },
      { width: 720, zoom: 1, theme: "dark", locale: "en" },
      { width: 900, zoom: 1.25, theme: "light", locale: "en" },
    ]
    for (const options of cases) {
      const name = `w${options.width}-zoom${options.zoom}-${options.theme}-${options.locale}`
      const item = { ...options, name, startedAt: new Date().toISOString() }
      evidence.cases.push(item)
      const zh = options.locale === "zh"
      const labels = zh
        ? { search: "搜索 PDF 文本", page: "页码", zoomIn: "放大", zoomOut: "缩小", next: "下一个搜索结果", results: "3 个结果", pageStatus: page => `第 ${page} / 3 页` }
        : { search: "Search PDF text", page: "Page number", zoomIn: "Zoom in", zoomOut: "Zoom out", next: "Next search result", results: "3 results", pageStatus: page => `Page ${page} of 3` }
      await page.goto(`http://127.0.0.1:${address.port}/?${new URLSearchParams(Object.entries(options).map(([key, value]) => [key, String(value)]))}`)
      await page.getByText(labels.pageStatus(1), { exact: true }).waitFor()
      await page.getByLabel(labels.search, { exact: true }).waitFor({ state: "visible" })
      await page.waitForFunction(() => !document.querySelector("[data-pdf-search-field] input").disabled)
      await waitForRenderedPage(1)
      item.initial = await geometry()
      await page.screenshot({ path: path.join(fixtureRoot, `${name}-chrome.png`) })
      assertFits(item.initial)
      assert.equal(item.initial.thumbnailsVisible, options.width >= 704, "thumbnail rail must use reader-container width, not the wide viewport breakpoint")
      await page.getByLabel(labels.page, { exact: true }).fill("2")
      await page.getByLabel(labels.page, { exact: true }).press("Enter")
      await page.getByText(labels.pageStatus(2), { exact: true }).waitFor()
      await waitForRenderedPage(2)
      await page.getByRole("button", { name: labels.zoomIn, exact: true }).click()
      await page.getByText("125%", { exact: true }).waitFor()
      await waitForRenderedPage(2, 125)
      await page.getByRole("button", { name: labels.zoomOut, exact: true }).click()
      await page.getByText("100%", { exact: true }).waitFor()
      await waitForRenderedPage(2)
      await page.getByLabel(labels.search, { exact: true }).fill("needle")
      await page.getByText(labels.results, { exact: true }).waitFor()
      item.searched = await geometry()
      assertFits(item.searched)
      await page.getByRole("button", { name: labels.next, exact: true }).click()
      await page.getByText(labels.pageStatus(2), { exact: true }).waitFor()
      await waitForRenderedPage(2)
      // A normal-size PDF may fit the wide reader. Force a genuinely oversized
      // original through real zoom buttons before asserting independent scroll.
      for (const percent of [125, 150, 175, 200]) {
        await page.getByRole("button", { name: labels.zoomIn, exact: true }).click()
        await page.getByText(`${percent}%`, { exact: true }).waitFor()
        await waitForRenderedPage(2, percent)
      }
      item.enlarged = await geometry()
      assertFits(item.enlarged)
      item.document = await page.evaluate(() => {
        const scroll = document.querySelector("[data-pdf-canvas-scroll]")
        const documentPage = document.querySelector("[data-pdf-document]")
        const flow = document.querySelector("[data-pdf-page-flow]")
        const canvas = documentPage.querySelector("canvas")
        const annotation = documentPage.querySelector('[title="Fixture anchored highlight"]')
        const pixels = canvas.getContext("2d").getImageData(0, 0, canvas.width, canvas.height).data
        let redPixels = 0
        for (let index = 0; index < pixels.length; index += 4) {
          if (pixels[index] > 200 && pixels[index + 1] < 30 && pixels[index + 2] < 30) redPixels += 1
        }
        scroll.scrollLeft = 0
        const initiallyReachableLeft = documentPage.getBoundingClientRect().left >= scroll.getBoundingClientRect().left - 1
        scroll.scrollLeft = scroll.scrollWidth
        const documentRect = documentPage.getBoundingClientRect()
        const annotationRect = annotation.getBoundingClientRect()
        const percent = (part, total) => Math.round(part / total * 10_000) / 100
        return {
          redPixels, initiallyReachableLeft,
          clientWidth: scroll.clientWidth, scrollWidth: scroll.scrollWidth, scrollLeft: scroll.scrollLeft,
          filter: getComputedStyle(canvas).filter,
          whitePage: getComputedStyle(documentPage).backgroundColor,
          pageFlow: {
            separateFromDocument: flow !== documentPage && documentPage.parentElement === flow && flow.parentElement === scroll,
            onlyChildIsDocument: flow.children.length === 1 && flow.firstElementChild === documentPage,
            display: getComputedStyle(flow).display,
            background: getComputedStyle(flow).backgroundColor,
            documentMargins: [getComputedStyle(documentPage).marginLeft, getComputedStyle(documentPage).marginRight],
            annotationAnchoredToDocument: annotation.offsetParent === annotation.parentElement
              && annotation.parentElement.offsetParent === documentPage,
            annotationRect: [annotation.style.left, annotation.style.top, annotation.style.width, annotation.style.height],
            annotationBackground: getComputedStyle(annotation).backgroundColor,
            annotationRelativeRect: [
              percent(annotationRect.left - documentRect.left, documentRect.width),
              percent(annotationRect.top - documentRect.top, documentRect.height),
              percent(annotationRect.width, documentRect.width),
              percent(annotationRect.height, documentRect.height),
            ],
          },
        }
      })
      assert.ok(item.document.redPixels > 100, "the actual PDF red source pixels remain red in either chrome theme")
      assert.equal(item.document.filter, "none")
      assert.equal(item.document.whitePage, "rgb(255, 255, 255)")
      assert.deepEqual(item.document.pageFlow, {
        separateFromDocument: true, onlyChildIsDocument: true, display: "block",
        background: "rgba(0, 0, 0, 0)", documentMargins: ["0px", "0px"],
        annotationAnchoredToDocument: true, annotationRect: ["12%", "14%", "28%", "3%"],
        annotationBackground: "rgba(110, 231, 183, 0.35)",
        annotationRelativeRect: [12, 14, 28, 3],
      }, "page-flow owns external placement; the unchanged white PDF document owns its internal coordinates")
      assert.equal(item.document.initiallyReachableLeft, true, "oversized PDF pages must start at reachable left edge, not negative centered overflow")
      assert.ok(item.document.scrollWidth > item.document.clientWidth && item.document.scrollLeft > 0, "real PDF canvas scroll must be independent from chrome")
      await page.screenshot({ path: path.join(fixtureRoot, `${name}-search.png`) })
      item.ok = true
      item.finishedAt = new Date().toISOString()
    }

    // Even within the app shell, default appearance must retain its Portal classes
    // and layout: the opt-in CSS predicate must not leak through the new slots.
    await page.goto(`http://127.0.0.1:${address.port}/?appearance=default&width=1100`)
    await page.getByText("Page 1 of 3", { exact: true }).waitFor()
    evidence.portal = await page.evaluate(() => {
      const style = selector => getComputedStyle(document.querySelector(selector))
      return {
        toolbar: style("[data-pdf-toolbar]").display,
        pagination: style("[data-pdf-pagination]").display,
        zoom: style("[data-pdf-zoom]").display,
        pageFlow: style("[data-pdf-page-flow]").display,
        titleWhiteSpace: style("[data-pdf-title] h2").whiteSpace,
        canvas: style("[data-pdf-canvas-scroll]").display,
        toolbarBackground: style("[data-pdf-toolbar]").backgroundColor,
        canvasBackground: style("[data-pdf-canvas-scroll]").backgroundColor,
      }
    })
    assert.deepEqual(evidence.portal, {
      toolbar: "flex", pagination: "contents", zoom: "contents", pageFlow: "contents", titleWhiteSpace: "nowrap", canvas: "flex",
      toolbarBackground: "rgb(255, 255, 255)", canvasBackground: "rgb(238, 244, 239)",
    })
    assert.deepEqual(evidence.errors, [])
    evidence.ok = true
  } finally {
    if (page) {
      await page.screenshot({ path: path.join(fixtureRoot, "final-browser.png") }).catch(() => {})
      evidence.finalBody = await page.locator("body").innerText().catch(() => "unavailable")
    }
    await writeFile(path.join(fixtureRoot, "layout-evidence.json"), JSON.stringify(evidence, null, 2))
    console.log(`PDF browser layout evidence: ${fixtureRoot}`)
    await browser?.close()
    await server.close()
  }
})
