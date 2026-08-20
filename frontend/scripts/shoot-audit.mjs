import { chromium } from 'playwright'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const __dirname = dirname(fileURLToPath(import.meta.url))
const OUT = join(__dirname, '..', '..', 'screenshots')
const tag = process.argv[2] || 'audit'
const errors = []
const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1280, height: 820 }, deviceScaleFactor: 2 })
page.on('console', (m) => m.type() === 'error' && errors.push(m.text()))
page.on('pageerror', (e) => errors.push(String(e)))

try {
  await page.goto('http://localhost:5173/', { waitUntil: 'networkidle', timeout: 30000 })
  await page.waitForTimeout(1200)
  await page.screenshot({ path: join(OUT, `${tag}-1-products.png`) })

  await page.getByRole('button', { name: '零售店铺' }).click()
  await page.waitForTimeout(900)
  await page.screenshot({ path: join(OUT, `${tag}-2-shops.png`) })

  await page.getByRole('button', { name: '连接设置' }).click()
  await page.waitForTimeout(900)
  await page.screenshot({ path: join(OUT, `${tag}-3-settings.png`) })

  console.log('saved 3 screenshots with tag', tag)
} catch (e) {
  console.error('STEP FAILED:', e.message)
} finally {
  console.log('CONSOLE_ERRORS:', JSON.stringify(errors))
  await browser.close()
}
