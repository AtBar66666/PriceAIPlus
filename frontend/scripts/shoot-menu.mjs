import { chromium } from 'playwright'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { dirname, join } from 'node:path'

const __dirname = dirname(fileURLToPath(import.meta.url))
const OUT = join(__dirname, '..', '..', 'screenshots')
const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1120, height: 800 }, deviceScaleFactor: 2 })
await page.goto(pathToFileURL(join(__dirname, 'style-menu.html')).href)
await page.waitForTimeout(500)
for (const id of ['fa', 'fb', 'fc', 'fd']) {
  await page.locator(`#${id}`).screenshot({ path: join(OUT, `style-${id}.png`) })
}
console.log('menu shots saved')
await browser.close()
