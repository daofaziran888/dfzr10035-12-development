<template>
  <section class="page">
    <header class="page-head">
      <div>
        <h2>运营概览</h2>
        <p class="page-desc">汇总各业务模块的关键指标，先看总量再看异常。</p>
      </div>
    </header>
    <div class="stat-row">
      <article v-for="card in cards" :key="card.label" class="stat-card">
        <span class="stat-label">{{ card.label }}</span>
        <strong class="stat-value">{{ card.value }}</strong>
      </article>
    </div>
    <table class="data-table">
      <thead>
        <tr><th>业务模块</th><th>今日新增</th><th>待处理</th><th>异常量</th></tr>
      </thead>
      <tbody>
        <tr v-for="row in moduleRows" :key="row.name">
          <td>{{ row.name }}</td>
          <td>{{ row.created }}</td>
          <td>{{ row.pending }}</td>
          <td>{{ row.abnormal }}</td>
        </tr>
      </tbody>
    </table>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { fetchJson } from '@/api/client'

type Overview = {
  cards: { label: string; value: number }[]
  modules: { name: string; created: number; pending: number; abnormal: number }[]
}

const cards = ref<Overview['cards']>([])
const moduleRows = ref<Overview['modules']>([])

onMounted(async () => {
  try {
    const payload = await fetchJson<Overview>('/api/overview')
    cards.value = payload.cards
    moduleRows.value = payload.modules
  } catch {
    cards.value = [{"label": "业务模块", "value": 0}, {"label": "今日新增", "value": 0}]
    moduleRows.value = [{"name": "接口提供方", "created": 0, "pending": 0, "abnormal": 0}, {"name": "鉴权配置", "created": 0, "pending": 0, "abnormal": 0}, {"name": "接口定义", "created": 0, "pending": 0, "abnormal": 0}, {"name": "来源采集归属", "created": 0, "pending": 0, "abnormal": 0}, {"name": "限流熔断", "created": 0, "pending": 0, "abnormal": 0}, {"name": "请求批次", "created": 0, "pending": 0, "abnormal": 0}, {"name": "响应解析", "created": 0, "pending": 0, "abnormal": 0}, {"name": "Schema差异", "created": 0, "pending": 0, "abnormal": 0}, {"name": "结果缓存", "created": 0, "pending": 0, "abnormal": 0}, {"name": "重放队列", "created": 0, "pending": 0, "abnormal": 0}, {"name": "数据投递", "created": 0, "pending": 0, "abnormal": 0}, {"name": "订阅管理", "created": 0, "pending": 0, "abnormal": 0}, {"name": "接口监控", "created": 0, "pending": 0, "abnormal": 0}, {"name": "Webhook路由", "created": 0, "pending": 0, "abnormal": 0}, {"name": "传输加密", "created": 0, "pending": 0, "abnormal": 0}, {"name": "调用日志", "created": 0, "pending": 0, "abnormal": 0}, {"name": "数据版本", "created": 0, "pending": 0, "abnormal": 0}, {"name": "管道审计", "created": 0, "pending": 0, "abnormal": 0}, {"name": "治理报告", "created": 0, "pending": 0, "abnormal": 0}, {"name": "增量追踪", "created": 0, "pending": 0, "abnormal": 0}, {"name": "抽样校验", "created": 0, "pending": 0, "abnormal": 0}, {"name": "用量计费", "created": 0, "pending": 0, "abnormal": 0}, {"name": "任务调度", "created": 0, "pending": 0, "abnormal": 0}]
  }
})
</script>
