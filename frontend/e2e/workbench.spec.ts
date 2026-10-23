import { expect, test, type Page } from '@playwright/test'
import type { ApplicationSettingsDescriptor } from '../src/types'
import { intent, objectPage, run, summary } from '../src/test/blackboard'
import { deliverableTemplates } from '../src/test/deliverables'

const settings: ApplicationSettingsDescriptor = {
  prompts: [
    {
      name: 'worker',
      display_name: '智能成员',
      description: '执行任务',
      version: '1',
      body: '核对来源后输出结果。',
      customized: true,
      loaded: true,
    },
  ],
  runtime: {
    run_max_concurrency: 4,
    run_queue_capacity: 8,
    run_queue_retry_after_seconds: 5,
    worker_max_concurrency: 4,
    reviewer_max_concurrency: 3,
    review_strategy: 'adaptive',
    deep_review_risk_levels: ['high', 'critical'],
    deep_review_task_types: ['research', 'decision'],
    max_planned_subtasks: 4,
    max_repair_rounds: 1,
    node_timeout_seconds: 60,
    run_timeout_seconds: 300,
    max_artifact_chars: 20000,
    max_tool_result_chars: 8000,
    model_retry_attempts: 2,
  },
  restart_required: false,
  storage: 'local',
}
const completed = {
  ...run,
  status: 'succeeded',
  output: '分析报告已交付（已校验存储和结构）。',
  pending_intents: [],
}
const workflow = {
  default_engine: 'blackboard',
  accept_new_blackboard_runs: true,
  blackboard_templates: deliverableTemplates,
}
async function openNavigation(page: Page) {
  if (await page.getByRole('button', { name: '打开会话列表' }).isVisible())
    await page.getByRole('button', { name: '打开会话列表' }).click()
}
async function openSettings(page: Page) {
  await openNavigation(page)
  await page.getByRole('button', { name: '打开工作空间设置' }).click()
}
async function closeNavigation(page: Page) {
  if (await page.getByRole('dialog').isVisible()) await page.keyboard.press('Escape')
}

async function chooseMoreAction(page: Page, label: string) {
  await page.getByRole('button', { name: '更多操作' }).click()
  await page.getByRole('menuitem', { name: label, exact: true }).click()
}

const pageErrors = new WeakMap<Page, string[]>()
test.afterEach(async ({ page }) => expect(pageErrors.get(page) ?? []).toEqual([]))

test.beforeEach(async ({ page }) => {
  const errors: string[] = []
  pageErrors.set(page, errors)
  page.on('pageerror', (error) => errors.push(error.message))
  page.on('request', (request) => {
    if (!request.url().startsWith('http://127.0.0.1:4173/'))
      errors.push(`Unexpected external request: ${request.url()}`)
  })
  await page.route('**/health', (route) => route.fulfill({ json: { status: 'ok' } }))
  await page.route('**/v1/**', async (route) => {
    const url = new URL(route.request().url())
    if (url.pathname === '/v1/capabilities')
      return route.fulfill({
        json: {
          workflow,
          task_types: ['general', 'planning'],
          logical_models: ['planner-strong', 'worker-fast'],
          agents: [],
          skills: [],
        },
      })
    if (url.pathname === '/v1/settings') return route.fulfill({ json: settings })
    if (url.pathname.startsWith('/v1/settings/'))
      return route.fulfill({ json: { ...settings, restart_required: true } })
    if (url.pathname === '/v1/providers')
      return route.fulfill({ json: { items: [], restart_required: false } })
    if (url.pathname.startsWith('/v1/providers/'))
      return route.fulfill({ json: { restart_required: true } })
    if (url.pathname.endsWith('/events'))
      return route.fulfill({
        contentType: 'text/event-stream',
        body: `event: run\ndata: ${JSON.stringify(completed)}\n\n`,
      })
    if (url.pathname.endsWith('/blackboard/objects')) return route.fulfill({ json: objectPage })
    if (url.pathname.endsWith('/blackboard')) return route.fulfill({ json: summary })
    if (url.pathname === '/v1/runs' && route.request().method() === 'POST')
      return route.fulfill({ status: 202, json: { run_id: 'board', status_url: '/v1/runs/board' } })
    if (url.pathname === '/v1/runs')
      return route.fulfill({
        json: {
          items: [
            {
              run_id: 'board',
              engine: 'blackboard',
              status: 'succeeded',
              created_at: summary.evaluated_at,
              updated_at: summary.evaluated_at,
            },
          ],
          offset: 0,
          limit: 20,
        },
      })
    if (url.pathname === '/v1/runs/board') return route.fulfill({ json: completed })
    return route.fulfill({ status: 404, json: { detail: 'Unexpected test endpoint' } })
  })
  await page.goto('/')
  await expect(page.locator('.connection-status')).toHaveText('系统就绪')
  await page.evaluate(() => document.fonts.ready)
  await expect(page.locator('html')).toHaveClass(/dark/)
  // Every test checks runtime errors, including lazy overlays and input controls.
})

test('home explicit completion, IME and streamed task result', async ({ page }) => {
  const input = page.getByRole('textbox', { name: '任务目标' })
  await expect(page.getByRole('heading', { name: '今天，想一起完成什么？' })).toBeVisible()
  await expect(page.getByRole('button', { name: '发送任务' })).toBeDisabled()
  await expect(page.getByRole('button', { name: '发送任务' }).locator('svg')).toBeVisible()
  await input.fill('梳理一份项目启动计划')
  await expect(input).not.toHaveValue('')
  await page.getByRole('button', { name: '发送任务' }).click()
  await expect(page.getByText('请先选择黑板任务的完成条件。', { exact: true })).toBeVisible()
  await page.getByLabel('黑板任务完成条件').selectOption('analysis-report')
  const submitted: string[] = []
  page.on('request', (req) => {
    if (new URL(req.url()).pathname === '/v1/runs' && req.method() === 'POST')
      submitted.push(req.url())
  })
  await input.dispatchEvent('keydown', { key: 'Enter', code: 'Enter', isComposing: true })
  await input.press('Shift+Enter')
  await expect(input).toHaveValue(/\n/)
  expect(submitted).toHaveLength(0)
  const request = page.waitForRequest(
    (req) => new URL(req.url()).pathname === '/v1/runs' && req.method() === 'POST',
  )
  await input.press('Enter')
  expect((await request).postDataJSON()).toMatchObject({
    engine: 'blackboard',
    blackboard: workflow.blackboard_templates[0]!.input,
  })
  await expect(page.getByText(completed.output, { exact: true })).toBeVisible()
  await page.getByRole('button', { name: '查看过程与证据' }).click()
  await expect(page.getByRole('dialog', { name: '任务详情' })).toBeVisible()
  await expect(page.getByText('目标 · 仍有验收缺口')).toBeVisible()
  await page.keyboard.press('Escape')
  await expect(page.getByRole('dialog')).toHaveCount(0)
})

test('navigation, rename, persistent theme, search and confirmed local deletion', async ({
  page,
}) => {
  await chooseMoreAction(page, '切换到浅色模式')
  await expect(page.locator('html')).not.toHaveClass(/dark/)
  await closeNavigation(page)
  await page.reload()
  await expect(page.locator('html')).not.toHaveClass(/dark/)
  await openNavigation(page)
  await page.getByRole('textbox', { name: '搜索会话' }).fill('没有匹配的内容')
  await expect(page.getByText('没有找到相关会话')).toBeVisible()
  await page.getByRole('textbox', { name: '搜索会话' }).fill('')

  await page
    .getByRole('button', { name: /^重命名会话：/ })
    .first()
    .click()
  const renameDialog = page.getByRole('dialog', { name: '重命名会话', exact: true })
  const name = renameDialog.getByRole('textbox', { name: '会话名称', exact: true })
  await expect(name).toBeFocused()
  await expect(name).toHaveValue('新对话')
  await name.fill('   ')
  await expect(renameDialog.getByRole('button', { name: '保存名称' })).toBeDisabled()
  await name.press('Enter')
  await expect(renameDialog).toBeVisible()
  await name.fill('  项目规划  ')
  await name.dispatchEvent('keydown', { key: 'Enter', code: 'Enter', isComposing: true })
  await expect(renameDialog).toBeVisible()
  await name.press('Enter')
  await expect(renameDialog).toHaveCount(0)
  await expect(page.locator('.active-title')).toHaveText('项目规划')

  await page.getByRole('button', { name: '重命名会话：项目规划', exact: true }).click()
  await name.fill('取消后不应保存')
  await name.press('Escape')
  await expect(renameDialog).toHaveCount(0)
  await expect(page.locator('.active-title')).toHaveText('项目规划')

  await page.getByRole('button', { name: '置顶会话：项目规划', exact: true }).click()
  await page.getByRole('button', { name: '重命名会话：项目规划', exact: true }).first().click()
  await name.fill('项目验收')
  await renameDialog.getByRole('button', { name: '保存名称' }).click()
  await expect(renameDialog).toHaveCount(0)
  await closeNavigation(page)
  await page.reload()
  await expect(page.locator('.active-title')).toHaveText('项目验收')
  await openNavigation(page)
  await expect(
    page.getByRole('button', { name: '取消置顶会话：项目验收', exact: true }),
  ).toBeVisible()
  await page.getByRole('textbox', { name: '搜索会话' }).fill('项目验收')
  await expect(page.locator('.history-select').first()).toContainText('项目验收')
  await page.getByRole('textbox', { name: '搜索会话' }).fill('')
  const activeId = await page.evaluate(() =>
    localStorage.getItem('work-harness:active-conversation:v1'),
  )
  await page
    .getByRole('button', { name: /^删除会话：/ })
    .first()
    .click()
  await expect(page.getByRole('dialog', { name: '删除本地会话？' })).toBeVisible()
  await page.getByRole('button', { name: '保留会话' }).click()
  expect(
    await page.evaluate(() => localStorage.getItem('work-harness:active-conversation:v1')),
  ).toBe(activeId)
  await page
    .getByRole('button', { name: /^删除会话：/ })
    .first()
    .click()
  await page.getByRole('button', { name: '确认删除' }).click()
  expect(
    await page.evaluate(() => localStorage.getItem('work-harness:active-conversation:v1')),
  ).not.toBe(activeId)
})

test('model settings accept model names, defaults and save through the existing API', async ({
  page,
}) => {
  await openSettings(page)
  await page.getByRole('button', { name: '添加服务' }).click()
  await page.getByPlaceholder('例如 team-gateway').fill('test-service')
  await page.getByPlaceholder('输入域名、IP 或完整接口地址').fill('https://models.example.test/v1')
  const model = page.getByPlaceholder('输入模型名称后按回车')
  await model.fill('test-model')
  await model.press('Enter')
  const saved = page.waitForRequest(
    (req) => req.url().endsWith('/v1/providers/test-service') && req.method() === 'PUT',
  )
  await page.getByRole('button', { name: '保存设置', exact: true }).click()
  expect((await saved).postDataJSON()).toMatchObject({
    config: {
      allowed_models: ['test-model'],
      base_url: 'https://models.example.test/v1',
      enabled: false,
    },
  })
  await expect(
    page.getByText('设置已保存，请重启 Work Harness 使新配置生效', { exact: true }),
  ).toBeVisible()
})

test('mobile and desktop layouts fit the viewport, with usable history overlays', async ({
  page,
}) => {
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(
    true,
  )
  await page.getByRole('button', { name: '配置任务选项' }).click()
  await expect(page.getByLabel('风险等级')).toBeVisible()
  await page.getByLabel('风险等级').selectOption('high')
  await page.keyboard.press('Escape')
  await openNavigation(page)
  await page.getByRole('button', { name: '运行记录', exact: true }).click()
  await expect(page.getByRole('dialog', { name: '运行记录' })).toBeVisible()
  await page.getByRole('button', { name: '查看运行详情' }).click()
  await expect(page.getByRole('dialog', { name: '任务详情' })).toBeVisible()
  await expect(page.getByText('目标 · 仍有验收缺口')).toBeVisible()
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(
    true,
  )
})

test('prompt restoration and runtime settings keep their save contracts', async ({ page }) => {
  await page.goto('/settings?section=prompts')
  await expect(page.getByRole('textbox', { name: 'Agent 提示词' })).toHaveValue(
    settings.prompts[0]!.body,
  )
  await page.getByRole('textbox', { name: 'Agent 提示词' }).fill('请先核验再给出结论。')
  const promptSave = page.waitForRequest(
    (req) => req.url().endsWith('/v1/settings/prompts/worker') && req.method() === 'PUT',
  )
  await page.getByRole('button', { name: '保存提示词', exact: true }).click()
  expect((await promptSave).postDataJSON()).toEqual({ body: '请先核验再给出结论。' })
  await page.getByRole('button', { name: '恢复内置版本', exact: true }).click()
  const reset = page.waitForRequest(
    (req) => req.url().endsWith('/v1/settings/prompts/worker') && req.method() === 'DELETE',
  )
  await page.getByRole('button', { name: '恢复', exact: true }).click()
  await reset
  await page.goto('/settings?section=runtime')
  const concurrency = page.getByRole('spinbutton', { name: '同时运行的任务数', exact: true })
  await concurrency.fill('6')
  await concurrency.press('Tab')
  const runtimeSave = page.waitForRequest(
    (req) => req.url().endsWith('/v1/settings/runtime') && req.method() === 'PUT',
  )
  await page.getByRole('button', { name: '保存运行配置', exact: true }).click()
  expect((await runtimeSave).postDataJSON()).toMatchObject({
    ...settings.runtime,
    run_max_concurrency: 6,
  })
  await expect(page.getByText('运行配置正在等待重启', { exact: true })).toBeVisible()
})

test('offline connection can recover without losing the composed task', async ({ page }) => {
  const input = page.getByRole('textbox', { name: '任务目标' })
  await input.fill('这段目标需要保留。')
  await page.route('**/health', (route) =>
    route.fulfill({ status: 503, json: { detail: '服务暂不可用' } }),
  )
  await chooseMoreAction(page, '刷新运行状态')
  await expect(page.getByText('暂时无法连接服务。你的输入会保留。')).toBeVisible()
  await expect(input).toHaveValue('这段目标需要保留。')
  await page.route('**/health', (route) => route.fulfill({ json: { status: 'ok' } }))
  await page.getByRole('button', { name: '重新连接', exact: true }).click()
  await expect(page.locator('.connection-status')).toHaveText('系统就绪')
  await expect(input).toHaveValue('这段目标需要保留。')
})

for (const [protocol, adapter, endpoint] of [
  ['responses', 'openai_compatible', 'responses'],
  ['chat_completions', 'openai_compatible', 'chat/completions'],
  ['messages', 'anthropic', 'messages'],
]) {
  test(`discover, search and assign models using ${protocol}`, async ({ page }) => {
    await page.goto('/settings?section=models')
    await expect(
      page.getByRole('heading', { name: '模型连接', exact: true, level: 2 }),
    ).toBeVisible()
    await page.getByRole('button', { name: '添加服务' }).click()
    await page.getByPlaceholder('例如 team-gateway').fill('local-gateway')
    await page.getByPlaceholder('输入域名、IP 或完整接口地址').fill(`127.0.0.1:8080/v1/${endpoint}`)
    await expect(page.getByLabel('API 协议', { exact: true })).toHaveValue(protocol!)
    await page.getByPlaceholder('输入 API Key').fill('browser-test-key')
    await expect(page.getByText('请求地址', { exact: true })).toHaveCount(0)
    await expect(page.getByRole('checkbox', { name: '允许 HTTP 连接', exact: true })).toHaveCount(0)
    await expect(page.getByRole('checkbox', { name: '允许连接局域网或本机服务' })).toHaveCount(0)
    await page.route('**/v1/providers/models/discover', async (route) => {
      expect(route.request().postDataJSON()).toMatchObject({
        api_key: 'browser-test-key',
        config: {
          adapter,
          api_family: protocol,
          base_url: `127.0.0.1:8080/v1/${endpoint}`,
          enabled: false,
          allow_insecure_http: null,
          allow_private_network: null,
          allowed_models: [],
        },
      })
      await route.fulfill({
        json: {
          items: [
            { id: 'model-alpha', name: 'Alpha' },
            { id: 'model-beta', name: 'Beta' },
          ],
          truncated: false,
        },
      })
    })
    await page.getByRole('button', { name: '获取模型', exact: true }).click()
    await expect(page.getByRole('status').filter({ hasText: '已获取 2 个模型' })).toBeVisible()
    await page.getByRole('textbox', { name: '搜索上游模型' }).fill('beta')
    await expect(page.getByRole('checkbox', { name: 'model-alpha Alpha' })).toHaveCount(0)
    await page.getByRole('checkbox', { name: 'model-beta Beta' }).check()
    await expect(page.getByLabel('默认工作模型（推荐）')).toHaveValue('model-beta')
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
    const saved = page.waitForRequest(
      (req) => req.url().endsWith('/v1/providers/local-gateway') && req.method() === 'PUT',
    )
    await page.getByRole('button', { name: '保存设置', exact: true }).click()
    expect((await saved).postDataJSON()).toMatchObject({
      config: { adapter, api_family: protocol, allowed_models: ['model-beta'] },
      model_bindings: { 'planner-strong': 'model-beta', 'worker-fast': 'model-beta' },
    })
    await expect(page.getByPlaceholder('输入 API Key')).toHaveCount(0)
  })
}

test('discovery failure can retry and does not remove manually selected models', async ({
  page,
}) => {
  await page.goto('/settings?section=models')
  await page.getByRole('button', { name: '添加服务' }).click()
  await page.getByPlaceholder('例如 team-gateway').fill('retry-gateway')
  await page.getByPlaceholder('输入域名、IP 或完整接口地址').fill('https://gateway.example/v1')
  const manual = page.getByPlaceholder('输入模型名称后按回车')
  await manual.fill('manual-model')
  await manual.press('Enter')
  let attempts = 0
  await page.route('**/v1/providers/models/discover', (route) => {
    attempts++
    return route.fulfill(
      attempts === 1
        ? { status: 502, json: { detail: '上游暂时不可用，请稍后重试' } }
        : { json: { items: [], truncated: false } },
    )
  })
  await page.getByRole('button', { name: '获取模型', exact: true }).click()
  await expect(page.getByText('上游暂时不可用，请稍后重试')).toBeVisible()
  await expect(page.getByLabel('默认工作模型（推荐）')).toHaveValue('manual-model')
  await page.getByRole('button', { name: '获取模型', exact: true }).click()
  await expect(page.getByText('上游返回了空列表，可检查密钥权限或手动添加模型 ID。')).toBeVisible()
  await expect(page.getByLabel('默认工作模型（推荐）')).toHaveValue('manual-model')
})

test('changing the connection discards a stale model discovery response', async ({ page }) => {
  await page.goto('/settings?section=models')
  await page.getByRole('button', { name: '添加服务' }).click()
  const address = page.getByPlaceholder('输入域名、IP 或完整接口地址')
  await address.fill('https://old-gateway.example')
  let release: () => void = () => {}
  const pending = new Promise<void>((resolve) => {
    release = resolve
  })
  let received: () => void = () => {}
  const started = new Promise<void>((resolve) => {
    received = resolve
  })
  await page.route('**/v1/providers/models/discover', async (route) => {
    received()
    await pending
    await route.fulfill({
      json: { items: [{ id: 'stale-model', name: 'Stale' }], truncated: false },
    })
  })
  await page.getByRole('button', { name: '获取模型', exact: true }).click()
  await started
  await address.fill('https://new-gateway.example')
  release()
  await expect(page.getByRole('button', { name: '获取模型', exact: true })).toBeEnabled()
  await expect(page.getByRole('checkbox', { name: 'stale-model Stale' })).toHaveCount(0)
})

test('shared shell keeps widths, navigation and draft state across settings', async ({
  page,
  isMobile,
}, testInfo) => {
  const draft = '切换页面后仍然保留的任务草稿'
  await page.getByRole('textbox', { name: '任务目标' }).fill(draft)
  if (!isMobile) {
    const handle = page.locator('.workbench-shell > [data-slot="handle"]')
    const box = (await handle.boundingBox())!
    await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2)
    await page.mouse.down()
    await page.mouse.move(box.x + 40, box.y + box.height / 2, { steps: 8 })
    await page.mouse.up()
    expect((await page.locator('.workspace-sidebar').boundingBox())!.width).toBeGreaterThan(240)
  }
  const header = (await page.locator('.workspace-navbar').boundingBox())!
  const column = (await page.locator('.input-stage').boundingBox())!
  await openSettings(page)
  await expect(page).toHaveURL(/\/settings\?section=models$/)
  await expect(page.getByRole('dialog')).toHaveCount(0)
  await expect(page.getByRole('heading', { name: '模型连接', exact: true, level: 2 })).toBeVisible()
  const settingsHeader = (await page.locator('.workspace-navbar').boundingBox())!
  const settingsColumn = (await page.locator('.settings-main').boundingBox())!
  expect(settingsHeader.height).toBe(header.height)
  expect(settingsHeader.x).toBeCloseTo(header.x, 0)
  expect(settingsColumn.x).toBeCloseTo(column.x, 0)
  expect(settingsColumn.width).toBeCloseTo(column.width, 0)
  for (const [section, heading] of [
    ['prompts', 'Agent 提示词'],
    ['runtime', '运行配置'],
    ['models', '模型连接'],
  ]) {
    if (isMobile) await page.getByRole('button', { name: '打开设置导航' }).click()
    await page
      .getByRole('navigation', { name: '设置分类' })
      .getByRole('button', { name: new RegExp(heading!) })
      .click()
    await expect(page).toHaveURL(new RegExp(`section=${section}`))
    await expect(page.getByRole('heading', { name: heading!, exact: true, level: 2 })).toBeVisible()
    await expect(page.getByRole('dialog')).toHaveCount(0)
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  }
  await page.getByRole('button', { name: '添加服务' }).click()
  await expect(page.getByLabel('接口地址（Base URL）', { exact: true })).toBeVisible()
  await expect(page.getByPlaceholder('输入 API Key', { exact: true })).toBeVisible()
  await page.getByLabel('API 协议').selectOption('responses')
  await page.screenshot({ animations: 'disabled', path: testInfo.outputPath('settings-dark.png') })
  await chooseMoreAction(page, '切换到浅色模式')
  await expect(page.locator('html')).not.toHaveClass(/dark/)
  await page.screenshot({ animations: 'disabled', path: testInfo.outputPath('settings-light.png') })
  await page.getByRole('button', { name: '返回工作空间', exact: true }).click()
  await expect(page.getByRole('textbox', { name: '任务目标' })).toHaveValue(draft)
  await page.screenshot({ animations: 'disabled', path: testInfo.outputPath('home-light.png') })
})

test('protected workbench shows its address and accepts its own access key', async ({ page }) => {
  await page.route('**/v1/capabilities', (route) => {
    if (route.request().headers()['x-api-key'] === 'workbench-test-key') return route.fallback()
    return route.fulfill({ status: 401, json: { detail: 'Unauthorized' } })
  })
  await page.reload()
  const dialog = page.getByRole('dialog', { name: '工作台访问验证' })
  await expect(dialog).toBeVisible()
  await expect(dialog.getByLabel('工作台地址（Base URL）')).toHaveValue('http://127.0.0.1:4173')
  await expect(dialog.getByLabel('工作台地址（Base URL）')).not.toBeEditable()
  await dialog.getByLabel('工作台访问密钥', { exact: true }).fill('workbench-test-key')
  await dialog.getByRole('button', { name: '验证并连接' }).click()
  await expect(dialog).toHaveCount(0)
  await expect(page.locator('.connection-status')).toHaveText('系统就绪')
  await page.reload()
  await expect(page.locator('.connection-status')).toHaveText('系统就绪')
  await expect(dialog).toHaveCount(0)
})

test('long results scroll independently while the composer stays in view', async ({
  page,
}, testInfo) => {
  const longRun = { ...completed, output: '核验来源，整理依据。\n'.repeat(120) }
  await page.route('**/v1/runs/board/events', (route) =>
    route.fulfill({
      contentType: 'text/event-stream',
      body: `event: run\ndata: ${JSON.stringify(longRun)}\n\n`,
    }),
  )
  await page.route('**/v1/runs/board', (route) => route.fulfill({ json: longRun }))
  await page
    .getByRole('textbox', { name: '任务目标' })
    .fill('检查一个很长的任务，保留全过程与依据。'.repeat(30))
  await page.getByLabel('黑板任务完成条件').selectOption('analysis-report')
  await page.getByRole('button', { name: '发送任务' }).click()
  await expect(page.getByRole('button', { name: '查看过程与证据' })).toBeVisible()
  await expect
    .poll(() => page.locator('.conversation').evaluate((el) => el.scrollHeight > el.clientHeight))
    .toBe(true)
  await page.screenshot({
    animations: 'disabled',
    path: testInfo.outputPath('conversation-dark.png'),
  })
  for (const size of [
    { width: 320, height: 640 },
    { width: 768, height: 680 },
    { width: 1280, height: 720 },
  ]) {
    await page.setViewportSize(size)
    await expect(page.getByRole('textbox', { name: '任务目标' })).toBeInViewport()
    const input = (await page.locator('.input-stage').boundingBox())!
    expect(input.y + input.height).toBeLessThanOrEqual(size.height + 1)
    expect((await page.locator('.workspace-navbar').boundingBox())!.y).toBe(0)
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  }
  await page.getByRole('button', { name: '查看过程与证据' }).click()
  await expect(page.getByRole('dialog', { name: '任务详情' })).toBeVisible()
  await page.screenshot({ animations: 'disabled', path: testInfo.outputPath('inspector-dark.png') })
})

test('essential controls stay visible and advanced selections survive folding', async ({
  page,
}) => {
  await expect(page.locator('.workspace-rail')).toHaveCount(0)
  await expect(page.getByRole('button', { name: '设置', exact: true })).toHaveCount(0)
  await expect(page.getByRole('button', { name: '打开设置中心' })).toHaveCount(0)
  await expect(page.locator('.composer-options button')).toHaveCount(1)
  await expect(page.getByLabel('协作方式', { exact: true })).toHaveCount(0)
  await expect(page.getByLabel('黑板任务完成条件')).toBeVisible()
  await page.getByRole('button', { name: '配置任务选项' }).click()
  await page.getByLabel('协作方式', { exact: true }).selectOption('legacy')
  await page.getByLabel('风险等级').selectOption('high')
  await page.keyboard.press('Escape')
  await expect(page.getByLabel('黑板任务完成条件')).toHaveCount(0)
  await page.getByRole('button', { name: '配置任务选项' }).click()
  await expect(page.getByLabel('协作方式', { exact: true })).toHaveValue('legacy')
  await expect(page.getByLabel('风险等级')).toHaveValue('high')
  await page.getByLabel('协作方式', { exact: true }).selectOption('blackboard')
  await page.keyboard.press('Escape')
  await expect(page.getByLabel('黑板任务完成条件')).toBeVisible()

  await chooseMoreAction(page, '工作台访问验证')
  await expect(page.getByRole('dialog', { name: '工作台访问验证' })).toBeVisible()
  await expect(page.getByLabel('工作台地址（Base URL）')).toHaveValue('http://127.0.0.1:4173')
  await expect(page.getByLabel('工作台地址（Base URL）')).not.toBeEditable()
  await page.keyboard.press('Escape')
  await expect(page.getByRole('dialog', { name: '工作台访问验证' })).toHaveCount(0)
  await openSettings(page)
  await page.getByRole('button', { name: '添加服务' }).click()
  await expect(page.getByLabel('鉴权方式')).toBeHidden()
  await expect(page.getByRole('button', { name: '查看服务列表' })).toHaveCount(0)
  await page.getByText('高级连接设置', { exact: true }).click()
  await page.getByLabel('鉴权方式').selectOption('x_api_key')
  await page.getByLabel('密钥提供方式').selectOption('environment')
  await page.getByPlaceholder('例如 TEAM_MODEL_API_KEY').fill('TEST_UPSTREAM_KEY')
  await page.getByText('高级连接设置', { exact: true }).click()
  await page.getByPlaceholder('例如 team-gateway').fill('advanced-service')
  await page.getByPlaceholder('输入域名、IP 或完整接口地址').fill('gateway.example')
  const model = page.getByPlaceholder('输入模型名称后按回车')
  await model.fill('model-a')
  await model.press('Enter')
  const saved = page.waitForRequest(
    (req) => req.url().endsWith('/v1/providers/advanced-service') && req.method() === 'PUT',
  )
  await page.getByRole('button', { name: '保存设置', exact: true }).click()
  expect((await saved).postDataJSON()).toMatchObject({
    config: {
      auth_scheme: 'x_api_key',
      credential_source: 'environment',
      api_key_env: 'TEST_UPSTREAM_KEY',
    },
  })
})

test('settings expose retry only after a failed load', async ({ page }) => {
  let fail = true
  await page.route('**/v1/settings', (route) =>
    fail
      ? route.fulfill({ status: 503, json: { detail: '设置暂不可用' } })
      : route.fulfill({ json: settings }),
  )
  await page.goto('/settings?section=prompts')
  const retry = page.getByRole('button', { name: '重新读取', exact: true })
  await expect(retry).toBeVisible()
  fail = false
  await retry.click()
  await expect(page.getByRole('textbox', { name: 'Agent 提示词' })).toHaveValue(
    settings.prompts[0]!.body,
  )
  await expect(retry).toHaveCount(0)
})

const reasoningReport = {
  id: 'verified-model-a',
  fingerprint: 'test-fingerprint',
  checked_at: '2026-10-22T12:00:00Z',
  model_available: true,
  checks: [
    { effort: null, status: 'supported', detail: '默认调用通过', source: 'default' },
    { effort: 'low', status: 'supported', detail: '校验通过', source: 'parameter_validation' },
    {
      effort: 'high',
      status: 'supported',
      detail: '调用通过；渠道已确认',
      source: 'channel_confirmation',
    },
    { effort: 'xhigh', status: 'unsupported', detail: '上游拒绝', source: 'unknown' },
    { effort: 'max', status: 'unverified', detail: '参数未确认', source: 'unknown' },
    { effort: 'ultra', status: 'supported', detail: '校验通过', source: 'parameter_validation' },
  ],
}
async function openReasoningSettings(page: Page) {
  await page.goto('/settings?section=models')
  await page.getByRole('button', { name: '添加服务' }).click()
  await page.getByPlaceholder('例如 team-gateway').fill('reasoning-service')
  await page.getByPlaceholder('输入域名、IP 或完整接口地址').fill('gateway.example')
  const model = page.getByPlaceholder('输入模型名称后按回车')
  await model.fill('model-a')
  await model.press('Enter')
}

test('settings only expose tested effort and preserve saved channel evidence', async ({ page }) => {
  await openReasoningSettings(page)
  await expect(page.getByLabel('默认推理档位')).toBeDisabled()
  await page.route('**/v1/providers/models/verify', (route) =>
    route.fulfill({ json: reasoningReport }),
  )
  await page.getByText('更多验证选项', { exact: true }).click()
  await page.getByPlaceholder('例如 low, high, xhigh').fill('high')
  const verify = page.waitForRequest((req) => req.url().endsWith('/models/verify'))
  await page.getByRole('button', { name: '测试模型与推理', exact: true }).click()
  expect((await verify).postDataJSON()).toMatchObject({
    model: 'model-a',
    confirmed_efforts: ['high'],
  })
  await expect(page.getByLabel('默认推理档位')).toBeEnabled()
  await expect(page.getByLabel('默认推理档位').locator('option')).toHaveText([
    '默认（不指定）',
    'low',
    'high',
    'ultra',
  ])
  await page.getByLabel('默认推理档位').selectOption('high')
  const saved = page.waitForRequest(
    (req) => req.url().endsWith('/v1/providers/reasoning-service') && req.method() === 'PUT',
  )
  await page.getByRole('button', { name: '保存设置', exact: true }).click()
  const payload = (await saved).postDataJSON()
  expect(payload.config.model_settings['model-a']).toEqual({
    verification: reasoningReport,
    default_effort: 'high',
  })
  await page.route('**/v1/providers', (route) =>
    route.fulfill({
      json: {
        items: [{ name: 'reasoning-service', source: 'persistent', loaded: true, ...payload }],
        restart_required: false,
      },
    }),
  )
  await page.reload()
  await page.getByRole('button', { name: '编辑设置', exact: true }).click()
  await expect(page.getByLabel('默认推理档位')).toHaveValue('high')
  await page.getByText('更多验证选项', { exact: true }).click()
  await expect(page.getByPlaceholder('例如 low, high, xhigh')).toHaveValue('high')
  await expect(page.getByPlaceholder('上游支持的其他名称，用逗号分隔')).toHaveValue('ultra')
  await page.getByPlaceholder('输入域名、IP 或完整接口地址').fill('another.example')
  await expect(page.getByLabel('默认推理档位')).toBeDisabled()
  await expect(page.getByLabel('默认推理档位')).toHaveValue('')
})

test('switching models and connections discards stale reasoning probe results', async ({
  page,
}) => {
  await openReasoningSettings(page)
  const model = page.getByPlaceholder('输入模型名称后按回车')
  await model.fill('model-b')
  await model.press('Enter')
  let release!: () => void
  let started!: () => void
  const startedPromise = new Promise<void>((resolve) => {
    started = resolve
  })
  const blocked = new Promise<void>((resolve) => {
    release = resolve
  })
  await page.route('**/v1/providers/models/verify', async (route) => {
    started()
    await blocked
    await route.fulfill({ json: reasoningReport })
  })
  await page.getByRole('button', { name: '测试模型与推理', exact: true }).click()
  await startedPromise
  await expect(page.getByRole('button', { name: '保存设置', exact: true })).toBeDisabled()
  await page.getByLabel('测试模型', { exact: true }).selectOption('model-b')
  release()
  await expect(page.getByLabel('默认推理档位')).toBeDisabled()
  await expect(page.getByRole('button', { name: '保存设置', exact: true })).toBeEnabled()
  await page.getByLabel('测试模型', { exact: true }).selectOption('model-a')
  await expect(page.getByLabel('默认推理档位')).toBeDisabled()
})

test('composer model selection is on the right, persists and reaches the run request', async ({
  page,
}, testInfo) => {
  await page.route('**/v1/capabilities', (route) =>
    route.fulfill({
      json: {
        workflow,
        task_types: ['general'],
        logical_models: ['worker-fast'],
        agents: [],
        skills: ['repository-analysis', 'risk-audit'].map((id) => ({
          id,
          description: id,
          version: '1',
          logical_model: 'worker-fast',
          required_capabilities: [],
          allowed_tools: [],
          compatible_providers: ['*'],
          preferred_providers: [],
          available_providers: ['compatible', 'tested-gateway'],
          applies_to: ['analysis', 'research', 'execution', 'risk'],
        })),
        model_choices: [
          {
            provider: 'tested-gateway',
            model: 'gpt-5.6-sol',
            reasoning_effort: 'high',
            supported_efforts: ['low', 'high'],
          },
        ],
      },
    }),
  )
  await page.evaluate(() => {
    localStorage.setItem(
      'work-harness:conversations:v1',
      JSON.stringify([
        {
          id: 'saved-skill-routing',
          skillSelections: [{ skill_id: 'repository-analysis', provider: 'compatible' }],
          messages: [],
        },
      ]),
    )
    localStorage.setItem('work-harness:active-conversation:v1', 'saved-skill-routing')
  })
  await page.reload()
  const choice = page.getByLabel('当前任务模型')
  await choice.click()
  await page.getByRole('option', { name: 'gpt-5.6-sol · high', exact: true }).click()
  await page.reload()
  await expect(choice).toHaveText('gpt-5.6-sol · high')
  const modelBox = (await choice.boundingBox())!
  const sendBox = (await page.getByRole('button', { name: '发送任务' }).boundingBox())!
  expect(modelBox.x + modelBox.width).toBeLessThanOrEqual(sendBox.x)
  const criteria = (await page.locator('.composer-execution').boundingBox())!
  expect(modelBox.y).toBeGreaterThan(criteria.y + criteria.height)
  expect(Math.abs(modelBox.y - sendBox.y)).toBeLessThan(20)
  await choice.click()
  await expect(
    page.getByRole('option', { name: 'gpt-5.6-sol · high', exact: true }),
  ).toHaveAttribute('data-state', 'checked')
  await page.keyboard.press('Escape')
  await expect(page.getByLabel('默认推理档位')).toHaveCount(0)
  await page.getByRole('button', { name: '配置任务选项' }).click()
  await page.getByText('能力模块', { exact: false }).click()
  const analysisSkill = page.getByRole('button', { name: /项目资料分析/ })
  await expect(analysisSkill).toHaveAttribute('aria-pressed', 'true')
  const riskSkill = page.getByRole('button', { name: /风险检查/ })
  await riskSkill.click()
  await expect(riskSkill).toHaveAttribute('aria-pressed', 'true')
  await expect(page.getByLabel('能力模块使用的模型服务')).toHaveCount(0)
  await expect(page.getByText('自动选择模型服务', { exact: true })).toHaveCount(0)
  await page.keyboard.press('Escape')
  await page.screenshot({ animations: 'disabled', path: testInfo.outputPath('composer-model.png') })
  await page.getByRole('textbox', { name: '任务目标' }).fill('指定模型整理一份候选草稿')
  await page.getByLabel('黑板任务完成条件').selectOption('analysis-report')
  const runRequest = page.waitForRequest(
    (req) => req.url().endsWith('/v1/runs') && req.method() === 'POST',
  )
  await page.getByRole('button', { name: '发送任务' }).click()
  const submitted = (await runRequest).postDataJSON()
  expect(submitted.model_selection).toEqual({
    provider: 'tested-gateway',
    model: 'gpt-5.6-sol',
  })
  expect(submitted.skills).toEqual([
    { skill_id: 'repository-analysis', provider: null },
    { skill_id: 'risk-audit', provider: null },
  ])
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
})

test('an unavailable saved model is never silently replaced', async ({ page }) => {
  await page.route('**/v1/capabilities', (route) =>
    route.fulfill({
      json: {
        workflow,
        task_types: ['general'],
        logical_models: [],
        agents: [],
        skills: [],
        model_choices: [
          {
            provider: 'tested-gateway',
            model: 'model-a',
            reasoning_effort: null,
            supported_efforts: [],
          },
        ],
      },
    }),
  )
  await page.reload()
  await page.getByLabel('当前任务模型').click()
  await page.getByRole('option', { name: 'model-a · 默认', exact: true }).click()
  await page.route('**/v1/capabilities', (route) =>
    route.fulfill({
      json: {
        workflow,
        task_types: ['general'],
        logical_models: [],
        agents: [],
        skills: [],
        model_choices: [],
      },
    }),
  )
  await page.reload()
  await expect(page.getByLabel('当前任务模型')).toHaveText('model-a（需重新测试）')
  await page.getByLabel('当前任务模型').click()
  await expect(page.getByRole('option', { name: 'model-a（需重新测试）' })).toBeDisabled()
  await page.keyboard.press('Escape')
  await page.getByRole('textbox', { name: '任务目标' }).fill('保持模型选择')
  await page.getByLabel('黑板任务完成条件').selectOption('analysis-report')
  let submitted = false
  page.on('request', (req) => {
    if (req.url().endsWith('/v1/runs') && req.method() === 'POST') submitted = true
  })
  await page.getByRole('button', { name: '发送任务' }).click()
  await expect(
    page.getByText('所选模型不可用，请在设置页重新测试，或选择跟随项目设置', { exact: true }),
  ).toBeVisible()
  expect(submitted).toBe(false)
  await expect(page.getByRole('textbox', { name: '任务目标' })).toHaveValue('保持模型选择')
})
