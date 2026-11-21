<template>
  <section class="daily-panel">
    <header class="daily-head">
      <div>
        <h3>质量日报取数</h3>
        <p class="page-desc">
          按业务发生日归窗：昨晚发生、今天才收到的事件仍计入发生当天；库表精确匹配，同名表不串库；
          同一事件的并发更正只计一次。
        </p>
      </div>
      <form class="daily-form" @submit.prevent="generate">
        <label class="filter-item">
          <span>业务日</span>
          <input v-model="reportDate" data-testid="daily-date" type="date" required />
        </label>
        <label class="filter-item">
          <span>库名（可选）</span>
          <input v-model="dbName" data-testid="daily-db" placeholder="如 order_db" />
        </label>
        <label class="filter-item">
          <span>表名（可选）</span>
          <input v-model="tableName" data-testid="daily-table" placeholder="如 ods_order_info" />
        </label>
        <button class="btn primary" data-testid="daily-generate" type="submit" :disabled="loading">
          {{ loading ? '生成中…' : '生成日报' }}
        </button>
      </form>
    </header>

    <p v-if="errorMessage" class="error-text">{{ errorMessage }}</p>

    <template v-if="report">
      <div class="stat-row">
        <article class="stat-card">
          <span class="stat-label">窗口归属</span>
          <strong class="stat-value">{{ report.归属日期 }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">确认事件</span>
          <strong class="stat-value">{{ report.质量事件总数 }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">合并重复记录</span>
          <strong class="stat-value">{{ report.合并重复记录数 }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">待核对旧记录</span>
          <strong class="stat-value">{{ report.待核对数量 }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">派工单</span>
          <strong class="stat-value">{{ report.派工清单数 }}</strong>
        </article>
      </div>

      <p class="daily-meta">
        报告 <strong>{{ report.报告编号 }}</strong> · 窗口快照
        <strong data-testid="daily-snapshot">{{ report.窗口快照编号 }}</strong> ·
        {{ report.时间窗口 }} · {{ report.筛选口径 }}
      </p>

      <div class="daily-columns">
        <div>
          <h4>窗口明细（按归属日期）</h4>
          <table class="data-table" data-testid="daily-details">
            <thead>
              <tr>
                <th>事件编号</th><th>库.表</th><th>归属日期</th><th>发生时间</th>
                <th>接收时间</th><th>跨日补入</th><th>合并记录</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="item in report.窗口明细" :key="`${item.事件编号}-${item.源记录id}`">
                <td>{{ item.事件编号 }}</td>
                <td>{{ item.库名 || '—' }}.{{ item.表名 || '—' }}</td>
                <td>{{ item.归属日期 }}</td>
                <td>{{ item.发生时间 }}</td>
                <td>{{ item.接收时间 }}</td>
                <td>{{ item.是否跨日补入 ? '是' : '否' }}</td>
                <td>{{ item.合并记录id.length ? item.合并记录id.join('、') : '—' }}</td>
              </tr>
              <tr v-if="!report.窗口明细.length">
                <td colspan="7" class="empty-state">该业务日窗口内没有确认事件</td>
              </tr>
            </tbody>
          </table>
        </div>
        <div>
          <h4>待核对旧记录（库表身份不完整）</h4>
          <table class="data-table" data-testid="daily-manual">
            <thead>
              <tr><th>事件编号</th><th>库</th><th>表</th><th>发生时间</th><th>原因</th></tr>
            </thead>
            <tbody>
              <tr v-for="item in report.待核对" :key="`manual-${item.事件编号}-${item.源记录id}`">
                <td>{{ item.事件编号 }}</td>
                <td>{{ item.库名 || '—' }}</td>
                <td>{{ item.表名 || '—' }}</td>
                <td>{{ item.发生时间 || '—' }}</td>
                <td>{{ item.原因 }}</td>
              </tr>
              <tr v-if="!report.待核对.length">
                <td colspan="5" class="empty-state">没有需要单独核对的旧记录</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>

      <h4>值班派工清单</h4>
      <table class="data-table" data-testid="daily-dispatch">
        <thead>
          <tr>
            <th>派工单号</th><th>事件编号</th><th>库.表</th><th>归属日期</th>
            <th>级别</th><th>跨日补入</th><th>领取状态</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="item in report.派工清单" :key="item.派工单号">
            <td>{{ item.派工单号 }}</td>
            <td>{{ item.事件编号 }}</td>
            <td>{{ item.库名 || '—' }}.{{ item.表名 || '—' }}</td>
            <td>{{ item.归属日期 }}</td>
            <td>{{ item.严重级别 }}</td>
            <td>{{ item.是否跨日补入 ? '是' : '否' }}</td>
            <td>{{ item.领取状态 }}</td>
          </tr>
          <tr v-if="!report.派工清单.length">
            <td colspan="7" class="empty-state">该窗口没有需要派工的事件</td>
          </tr>
        </tbody>
      </table>

      <div class="page-actions">
        <button class="btn" data-testid="daily-claim" type="button" @click="claim">
          领取本日报派工
        </button>
        <span v-if="claimMessage" class="daily-meta">{{ claimMessage }}</span>
      </div>
    </template>
  </section>
</template>

<script setup lang="ts">
import { ref } from 'vue'

import {
  claimReportDispatches,
  generateDailyReport,
  type DailyReport,
} from '@/api/quality-daily'

const today = new Date().toISOString().slice(0, 10)
const reportDate = ref(today)
const dbName = ref('')
const tableName = ref('')
const loading = ref(false)
const errorMessage = ref('')
const claimMessage = ref('')
const report = ref<DailyReport | null>(null)

async function generate() {
  loading.value = true
  errorMessage.value = ''
  claimMessage.value = ''
  try {
    report.value = await generateDailyReport(reportDate.value, {
      dbName: dbName.value.trim() || undefined,
      tableName: tableName.value.trim() || undefined,
    })
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '质量日报生成失败'
  } finally {
    loading.value = false
  }
}

async function claim() {
  if (!report.value) return
  claimMessage.value = ''
  try {
    const result = await claimReportDispatches(report.value.报告编号)
    const dates = Object.entries(result.按归属日期)
      .map(([day, count]) => `${day}: ${count} 条`)
      .join('，')
    claimMessage.value = `${result.领取人} 领取 ${result.领取事件数} 条（${dates || '无'}）`
  } catch (error) {
    claimMessage.value = error instanceof Error ? error.message : '领取失败'
  }
}
</script>

<style scoped>
.daily-panel { margin: 16px 0 24px; padding: 16px; background: #fff; border: 1px solid var(--border); border-radius: 10px; }
.daily-head { display: flex; justify-content: space-between; gap: 16px; align-items: flex-start; flex-wrap: wrap; }
.daily-head h3 { margin: 0 0 4px; }
.daily-form { display: flex; gap: 10px; align-items: flex-end; flex-wrap: wrap; }
.daily-meta { color: var(--muted); font-size: 12px; margin: 8px 0; }
.daily-columns { display: grid; grid-template-columns: 1fr; gap: 16px; }
.daily-columns h4, .daily-panel > h4 { margin: 14px 0 6px; }
</style>
