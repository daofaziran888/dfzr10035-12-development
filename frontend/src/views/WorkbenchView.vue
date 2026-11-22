<template>
  <section class="page" :data-module="meta.key">
    <header class="page-head">
      <div>
        <h2>{{ meta.name }}</h2>
        <p class="page-desc">{{ meta.desc }}</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="open-create" type="button" @click="openCreate">
          登记{{ meta.entity }}
        </button>
        <button class="btn" data-testid="export" type="button" @click="exportRows">
          导出{{ meta.name }}清单
        </button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <p class="status-legend">
      <span v-for="item in statusSummary" :key="item.status" class="legend-item">
        {{ item.status }}：{{ item.count }}
      </span>
    </p>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :data-testid="`filter-${field}`" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" data-testid="query" type="submit">查询</button>
      <button class="btn ghost" data-testid="reset" type="button" @click="resetFilters">重置条件</button>
    </form>

    <p v-if="sourceNote" class="status-legend source-note" data-testid="source-note">
      {{ sourceNote }}
    </p>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in meta.fields" :key="column">{{ column }}</th>
          <th>当前状态</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :data-testid="`row-${row.id}`" :data-status="String(row.status)">
          <td v-for="column in meta.fields" :key="column">
            <template v-if="column === '历史名称'">
              <span v-if="row.历史名称">{{ row.历史名称 }}</span>
              <span v-else class="muted">—</span>
            </template>
            <template v-else>{{ row[column] ?? '—' }}</template>
          </td>
          <td>{{ row.status }}</td>
          <td class="row-actions">
            <button
              v-for="action in availableActions(row)"
              :key="action"
              class="link"
              type="button"
              :data-testid="`action-${row.id}-${action}`"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="meta.fields.length + 2" class="empty-state">
            暂无{{ meta.name }}数据，可先登记{{ meta.entity }}
          </td>
        </tr>
      </tbody>
    </table>

    <div v-if="pendingAction" class="modal-backdrop" data-testid="action-modal">
      <form class="modal" @submit.prevent="submitAction">
        <h3>{{ pendingAction.action }} · {{ pendingAction.row.批次编号 || pendingAction.row.归属编号 || pendingAction.row.记录编号 || pendingAction.row.提供方编号 || pendingAction.row.id }}</h3>
        <label v-for="input in pendingInputs" :key="input.field" class="create-field">
          <span>{{ input.label }}</span>
          <input v-model="actionValues[input.field]" :data-testid="`action-input-${input.field}`" :required="input.required" />
        </label>
        <label class="create-field">
          <span>备注 / 依据（写入待核理由或变更原因）</span>
          <input v-model="actionRemark" data-testid="action-remark" />
        </label>
        <div class="page-actions">
          <button class="btn" type="button" @click="pendingAction = null">取消</button>
          <button class="btn primary" data-testid="submit-action" type="submit" :disabled="acting">
            {{ acting ? '执行中…' : '确认执行' }}
          </button>
        </div>
      </form>
    </div>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>登记{{ meta.entity }}</h3>
        <label v-for="field in meta.fields" :key="field" class="create-field">
          <span>{{ field }}</span>
          <input
            v-model="createValues[field]"
            :data-testid="`create-${field}`"
            :required="isRequiredField(field)"
          />
        </label>
        <div class="page-actions">
          <button class="btn" type="button" @click="showCreate = false">取消</button>
          <button class="btn primary" data-testid="submit-create" type="submit" :disabled="creating">
            {{ creating ? '提交中…' : '确认登记' }}
          </button>
        </div>
      </form>
    </div>

    <footer class="page-foot">
      <span>共 {{ total }} 条{{ meta.name }}记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import {
  createEntry,
  downloadEntries,
  listEntries,
  moduleMeta,
  runAction as applyAction,
} from '@/api/backend-service'
import type { EntryRow } from '@/data/types'

// 标记类动作即使后端没有声明入参，也需要人写清依据。
const REQUIRES_REMARK = new Set(['标记待核', '作废归属', '驳回记录'])

const props = defineProps<{ moduleKey: string }>()
const meta = moduleMeta(props.moduleKey)
const rows = ref<EntryRow[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = meta.fields.slice(0, 3)
const showCreate = ref(false)
const creating = ref(false)
const createValues = ref<Record<string, string>>({})
const pendingAction = ref<{ action: string; row: EntryRow } | null>(null)
const actionValues = ref<Record<string, string>>({})
const actionRemark = ref('')
const acting = ref(false)
const pendingInputs = computed(() => {
  if (!pendingAction.value) return []
  return meta.actionInputs?.[pendingAction.value.action] ?? []
})
// 来源类模块在页面上先说明「按编号判定、按名称展示」的规则，避免把同名路径当同一来源。
const sourceNote = computed(() => {
  if (meta.key === 'source_attribution') {
    return '归属判定只认端点编号与提供方编号；端点展示名称与提供方名称为展示用，改名后旧名进入历史名称仍可回溯。'
  }
  if (meta.key === 'request_batch') {
    return '批次登记的是归属清单里的实际目标：同一路径被多家提供方登记时，只填路径无法启动，需按归属编号或端点编号指定来源。'
  }
  return ''
})
const stats = computed(() => [
  { label: meta.metrics[0], value: total.value },
  { label: meta.metrics[1], value: rows.value.filter((row) => row.status === meta.statuses[1]).length },
  {
    label: meta.metrics[2],
    value: rows.value.filter((row) => row.status === meta.statuses[meta.statuses.length - 1]).length,
  },
])
const statusSummary = computed(() =>
  meta.statuses.map((status) => ({
    status,
    count: rows.value.filter((row) => String(row.status) === status).length,
  })),
)

// 缺省沿用「全部必填」；声明了 requiredFields 的模块只校验真正关键的字段。
function isRequiredField(field: string): boolean {
  return meta.requiredFields ? meta.requiredFields.includes(field) : true
}

function resetFilters() {
  filters.value = {}
  void reload()
}

async function exportRows() {
  errorMessage.value = ''
  try {
    await downloadEntries(meta.key, filters.value)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '导出失败'
  }
}

function openCreate() {
  createValues.value = Object.fromEntries(meta.fields.map((field) => [field, '']))
  errorMessage.value = ''
  showCreate.value = true
}

async function submitCreate() {
  creating.value = true
  errorMessage.value = ''
  try {
    const result = await createEntry(meta.key, createValues.value)
    if (!result.ok) throw new Error(result.message)
    showCreate.value = false
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '登记失败'
  } finally {
    creating.value = false
  }
}

// 重试只对执行失败（部分失败）的批次开放，避免把待执行批次当成失败重放。
function availableActions(row: EntryRow): string[] {
  if (meta.key !== 'request_batch') return meta.actions
  return meta.actions.filter(
    (action) => action !== '重试批次' || row.status === '部分失败',
  )
}

function runAction(action: string, row: EntryRow) {
  // 状态流转动作直接执行；需要填参的动作先弹窗收集依据，再交给后端判定。
  const inputs = meta.actionInputs?.[action] ?? []
  if (!inputs.length && !REQUIRES_REMARK.has(action)) {
    void dispatchAction(action, row, {}, '')
    return
  }
  pendingAction.value = { action, row }
  actionValues.value = Object.fromEntries(inputs.map((input) => [input.field, '']))
  actionRemark.value = ''
  errorMessage.value = ''
}

async function submitAction() {
  if (!pendingAction.value) return
  const { action, row } = pendingAction.value
  const missing = pendingInputs.value
    .filter((input) => input.required && !String(actionValues.value[input.field] ?? '').trim())
    .map((input) => input.label)
  if (missing.length) {
    errorMessage.value = `请填写：${missing.join('、')}`
    return
  }
  const values: Record<string, string> = {}
  for (const input of pendingInputs.value) {
    if (input.field !== 'remark') values[input.field] = String(actionValues.value[input.field] ?? '').trim()
  }
  await dispatchAction(action, row, values, actionRemark.value.trim())
}

async function dispatchAction(
  action: string,
  row: EntryRow,
  values: Record<string, string>,
  remark: string,
) {
  acting.value = true
  errorMessage.value = ''
  try {
    const result = await applyAction(meta.key, Number(row.id), action, values, remark)
    if (!result.ok) throw new Error(result.message)
    pendingAction.value = null
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '操作失败'
  } finally {
    acting.value = false
  }
}

async function reload() {
  errorMessage.value = ''
  try {
    const payload = await listEntries(meta.key, filters.value)
    rows.value = payload.items
    total.value = payload.total
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '业务列表读取失败'
  }
}

onMounted(reload)
</script>
