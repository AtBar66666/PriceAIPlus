import { chromium } from 'playwright'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const __dirname = dirname(fileURLToPath(import.meta.url))
const OUT = join(__dirname, '..', '..', 'screenshots')

const errors = []
const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1512, height: 950 }, deviceScaleFactor: 2 })
page.on('console', (m) => m.type() === 'error' && errors.push(m.text()))
page.on('pageerror', (e) => errors.push(String(e)))

try {
  await page.goto('http://localhost:5173/', { waitUntil: 'networkidle', timeout: 30000 })
  await page.locator('.ant-menu-item', { hasText: '货源广场' }).click()
  await page.waitForTimeout(600)
  const box = page.locator('input[placeholder*="bugteam"]')
  await box.click()
  await box.fill('bugteam')
  await page.keyboard.press('Enter')
  // 等实时搜索结果
  await page.waitForSelector('.lx-row', { timeout: 25000 })
  await page.waitForTimeout(1200)
  await page.screenshot({ path: join(OUT, '07-live-bugteam.png'), fullPage: true })
  console.log('saved 07-live-bugteam.png')

  // 打开第一行查看当前商品详情
  await page.locator('.lx-row').first().click()
  await page.waitForSelector('.ant-drawer canvas', { timeout: 15000 })
  await page.waitForTimeout(1200)
  await page.screenshot({ path: join(OUT, '08-live-detail.png') })
  console.log('saved 08-live-detail.png')
} catch (e) {
  console.error('STEP FAILED:', e.message)
} finally {
  console.log('CONSOLE_ERRORS:', JSON.stringify(errors))
  await browser.close()
}
