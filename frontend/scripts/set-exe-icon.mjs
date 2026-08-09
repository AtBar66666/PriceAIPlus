import { rcedit } from 'rcedit'

const [exePath, iconPath] = process.argv.slice(2)

if (!exePath || !iconPath) {
  throw new Error('Usage: node set-exe-icon.mjs <exe-path> <ico-path>')
}

await rcedit(exePath, { icon: iconPath })
