import { chromium } from 'playwright'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const __dirname = dirname(fileURLToPath(import.meta.url))
const OUT = join(__dirname, '..', '..', 'screenshots')
const errors = []
const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1440, height: 940 }, deviceScaleFactor: 2 })
page.on('console', (m) => m.type() === 'error' && errors.push(m.text()))
page.on('pageerror', (e) => errors.push(String(e)))

try {
  await page.goto('http://localhost:5173/', { waitUntil: 'networkidle', timeout: 30000 })
  await page.waitForTimeout(1600)
  // viewport-scale (not fullPage) so we judge真实渲染尺寸
  await page.screenshot({ path: join(OUT, '15-top-viewport.png') })
  console.log('saved 15-top-viewport.png')
} catch (e) {
  console.error('STEP FAILED:', e.message)
} finally {
  console.log('CONSOLE_ERRORS:', JSON.stringify(errors))
  await browser.close()
}
