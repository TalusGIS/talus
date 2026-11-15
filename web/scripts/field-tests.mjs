// Runs the Talus Field unit tests. Talus Field is part of Talus+, and its source is not in the
// open-source tree — there, this says so and exits cleanly instead of failing on a missing folder.
import { spawnSync } from 'node:child_process'
import { existsSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

const fieldDir = fileURLToPath(new URL('../../paid/web/src/field', import.meta.url))
if (!existsSync(fieldDir)) {
  console.log('Talus Field is part of Talus+ and is not in this tree: no Field tests to run.')
  process.exit(0)
}
const run = spawnSync(process.execPath, ['--test', '../paid/web/src/field/**/*.test.ts'], { stdio: 'inherit' })
process.exit(run.status ?? 1)
