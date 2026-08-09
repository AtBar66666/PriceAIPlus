import { chromium } from 'playwright'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const __dirname = dirname(fileURLToPath(import.meta.url))
const OUT = join(__dirname, '..', '..', 'screenshots')
const errors = []
const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1280, height: 800 }, deviceScaleFactor: 2 })
page.on('console', (m) => m.type() === 'error' && errors.push(m.text()))
page.on('pageerror', (e) => errors.push(String(e)))

try {
  await page.goto('http://localhost:5173/', { waitUntil: 'networkidle', timeout: 30000 })
  await page.waitForTimeout(1600)
  // click first product row to open the drawer
  await page.locator('div.group.cursor-pointer').first().click()
  await page.waitForTimeout(1800)
  await page.screenshot({ path: join(OUT, '14-drawer.png') })
  console.log('saved 14-drawer.png')
} catch (e) {
  console.error('STEP FAILED:', e.message)
} finally {
  console.log('CONSOLE_ERRORS:', JSON.stringify(errors))
  await browser.close()
}
