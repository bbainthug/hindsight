/**
 * Personal Brain's read-only local extension catalog for DeepSeek Harness.
 *
 * The MCP client entries are added by cordis.patch.yml. This module only makes
 * the bundled skills discoverable as native DSH skills.
 */

import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const PLUGIN_DIR = dirname(fileURLToPath(import.meta.url))

const SKILLS = [
  {
    dir: 'personal-brain-extensions',
    name: 'personal-brain-extensions',
    description:
      'Inspect the local Codex and DeepSeek Harness skill/plugin inventory through Personal Brain.',
    whenToUse:
      'Use when the user asks which local skills or plugins exist, whether an extension is active, '
      + 'or how Codex and DSH are connected.',
  },
  {
    dir: 'hindsight-recall',
    name: 'hindsight-recall',
    description:
      'Search the user\'s Hindsight conversation history for past actions, statements, and decisions.',
    whenToUse:
      'Use when the user asks what they previously did, said, decided, or discussed '
      + '("我之前做过/说过/决定过什么"), or when a claim about past work needs a dated source.',
  },
]

export const name = 'personal-brain-dsh'
export const inject = ['skills']

export function apply(ctx) {
  for (const skill of SKILLS) {
    const skillDir = join(PLUGIN_DIR, 'skills', skill.dir)
    let content
    try {
      content = readFileSync(join(skillDir, 'SKILL.md'), 'utf8')
        .replace(/^\uFEFF?---[\s\S]*?---\s*/u, '')
        .trim()
    } catch (error) {
      ctx.logger?.warn?.(`[personal-brain] could not read runtime skill ${skill.name}: ${error.message}`)
      continue
    }

    ctx.skills.register({
      name: skill.name,
      description: skill.description,
      whenToUse: skill.whenToUse,
      content,
      source: 'runtime',
      invocation: { modelInvocable: true, userInvocable: true },
      resourceBase: { kind: 'directory', path: skillDir },
    })
  }
}
