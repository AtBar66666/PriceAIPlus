import { chromium } from 'playwright'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const __dirname = dirname(fileURLToPath(import.meta.url))
const OUT = join(__dirname, '..', '..', 'screenshots')
const errors = []
const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1200, height: 800 } })
page.on('console', (m) => m.type() === 'error' && errors.push(m.text()))
page.on('pageerror', (e) => errors.push('PAGEERROR: ' + String(e)))

try {
  const resp = await page.goto('http://localhost:5173/', { waitUntil: 'networkidle', timeout: 20000 })
  console.log('HTTP', resp?.status())
  await page.waitForTimeout(1500)
  const rootHtmlLen = await page.evaluate(() => document.getElementById('root')?.innerHTML.length ?? 0)
  const title = await page.title()
  const bodyText = (await page.evaluate(() => document.body.innerText || '')).slice(0, 120)
  console.log('title:', title)
  console.log('root innerHTML length:', rootHtmlLen)
  console.log('body text head:', JSON.stringify(bodyText))
  await page.screenshot({ path: join(OUT, 'check-load.png') })
  console.log('screenshot: check-load.png')
} catch (e) {
  console.error('LOAD FAILED:', e.message)
} finally {
  console.log('CONSOLE_ERRORS:', JSON.stringify(errors))
  await browser.close()
}
