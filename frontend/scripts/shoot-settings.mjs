import { chromium } from 'playwright';

(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 }, deviceScaleFactor: 2 });
  await page.goto('http://localhost:5173/');
  await page.waitForTimeout(1000);

  // click settings
  await page.click('text=设置');
  await page.waitForTimeout(1000);
  await page.screenshot({ path: '../screenshots/13-settings.png' });

  await browser.close();
})();
