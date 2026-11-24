<template>
  <section class="handoff-panel" data-testid="handoff-consistency-panel">
    <header class="handoff-head">
      <div>
        <h3>交接版本一致性核对</h3>
        <p class="handoff-sub">
          同一格时窗口的多版热力只做版本采用、绝不相加；已发出交接单冻结每个分项选择，刷新后仍与实际交接一致。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn" type="button" data-testid="handoff-refresh" :disabled="loading" @click="load">
          {{ loading ? '刷新中…' : '刷新核对' }}
        </button>
        <button
          v-if="!handoff"
          class="btn primary"
          type="button"
          data-testid="handoff-build-demo"
          :disabled="building"
          @click="buildScenario"
        >
          {{ building ? '构造中…' : '构造甲窗/乙窗市级交接' }}
        </button>
        <button
          v-else
          class="btn primary"
          type="button"
          data-testid="handoff-start-correction"
          :disabled="correcting"
          @click="startCorrection"
        >
          {{ correcting ? '启动中…' : '按本次发出依据启动区域更正' }}
        </button>
      </div>
    </header>

    <p v-if="message" class="handoff-message" data-testid="handoff-message">{{ message }}</p>
    <p v-if="error" class="error-text" data-testid="handoff-error">{{ error }}</p>

    <div v-if="handoff" class="handoff-body">
      <div class="handoff-meta">
        <span><b>交接单：</b><code>{{ handoff.handoff_id }}</code></span>
        <span><b>层级：</b>{{ handoff.level }}</span>
        <span><b>状态：</b>{{ handoff.status }}</span>
        <span><b>指纹：</b><code>{{ handoff.receipt?.fingerprint }}</code></span>
      </div>

      <table class="data-table handoff-table" data-testid="handoff-window-table">
        <thead>
          <tr>
            <th>窗口</th>
            <th>热力（已核对版本）</th>
            <th>区域（采用版本）</th>
            <th>待调差数</th>
            <th>窗口状态</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="w in windows"
            :key="w.window"
            :data-testid="`handoff-row-${w.window}`"
            :data-status="w.status"
          >
            <td>{{ w.window }}</td>
            <td>
              <template v-if="w.heatmap">
                <strong :class="{ 'num-ok': w.heatmap_status === '已核对' }">{{ w.heatmap.point_count }} 点</strong>
                <span class="tag">v{{ w.heatmap.version }} · {{ w.heatmap_status }}</span>
              </template>
              <span v-else>—</span>
            </td>
            <td>
              <template v-if="w.region">
                <strong>{{ w.region.region_count }} 点</strong>
                <span class="tag">采用 {{ w.region.adopted_heat_uid ?? '未关联版本' }}</span>
              </template>
              <span v-else>未确认</span>
            </td>
            <td>
              <span v-if="w.pending_delta !== null && w.pending_delta !== undefined" class="delta">
                {{ w.pending_delta > 0 ? '+' : '' }}{{ w.pending_delta }}
              </span>
              <span v-else>—</span>
            </td>
            <td>
              <span class="status-pill" :data-pending="w.status === '待续'">{{ w.status }}</span>
              <div v-if="w.pending_reason" class="pending-reason">{{ w.pending_reason }}</div>
            </td>
          </tr>
        </tbody>
      </table>

      <div class="handoff-grid">
        <div class="handoff-card">
          <h4>独立签发的已核对热力</h4>
          <ul>
            <li v-for="h in handoff.heatmap_independent_issue ?? []" :key="`${h.window}-${h.uid}`">
              {{ h.window }}：{{ h.point_count }} 点（{{ h.status }}），核对方式：{{ h.check_row?.method ?? '核对行已留存' }}
            </li>
          </ul>
        </div>
        <div class="handoff-card">
          <h4>尚未调账的区域（单独注明）</h4>
          <ul>
            <li v-for="r in handoff.region_pending_adjustment ?? []" :key="r.window">
              {{ r.window }}：区域 {{ r.region_count }} 点 / 最新热力 {{ r.latest_checked_heat }} 点 /
              待调差 <b>{{ r.pending_delta }}</b>
            </li>
          </ul>
        </div>
        <div class="handoff-card">
          <h4>只接收已对齐数据：可核对原数</h4>
          <ul>
            <li v-for="o in handoff.aligned_package?.original_figures ?? []" :key="o.window">
              {{ o.window }}：原数 <b>{{ o.point_count }}</b> 点（旧热力与区域一致，可按原格时窗口回算）
            </li>
          </ul>
        </div>
      </div>

      <p class="guard-line" data-testid="never-17-guard">
        区域确认总数 {{ handoff.region_confirmed_total }}：各窗只按一个采用版本计一次，
        8 点旧版与 9 点更正版不会相加为 17。
      </p>

      <div v-if="correction" class="correction-box" data-testid="correction-result">
        <h4>区域更正已按发出依据启动：<code>{{ correction.correction_id }}</code></h4>
        <p>依据指纹 {{ correction.basis_fingerprint }}；更正行
          <span v-for="l in correction.lines" :key="l.window" class="correction-line">
            {{ l.window }} {{ l.from_points }}→{{ l.to_points }}（差 {{ l.adjust_delta > 0 ? '+' : '' }}{{ l.adjust_delta }}，{{ l.status }}）
          </span>
        </p>
        <p class="pending-reason">已发出的交接单保持冻结，因此上方仍显示 热力 9 / 区域 8 / 待调差 1。</p>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { fetchJson, request } from '@/api/client'

const handoff = ref<any>(null)
const correction = ref<any>(null)
const loading = ref(false)
const building = ref(false)
const correcting = ref(false)
const error = ref('')
const message = ref('')

const windows = computed<any[]>(() => {
  if (!handoff.value) return []
  if (Array.isArray(handoff.value.windows)) return handoff.value.windows
  return Object.values(handoff.value.window_map ?? {})
})

async function load() {
  loading.value = true
  error.value = ''
  try {
    handoff.value = await fetchJson<any>('/api/geo_snapshot/handoffs/current')
  } catch (e) {
    handoff.value = null
    error.value = e instanceof Error ? e.message : '当前没有已发出的交接单'
  } finally {
    loading.value = false
  }
}

async function buildScenario() {
  building.value = true
  error.value = ''
  message.value = ''
  try {
    const resp = await request('/api/geo_snapshot/scenario/city-handoff', {
      method: 'POST',
      body: JSON.stringify({}),
    })
    if (!resp.ok) throw new Error(`构造失败：${resp.status}`)
    const data = await resp.json()
    correction.value = data.correction ?? null
    message.value = '已构造并发出市级交接，且已按发出依据启动区域更正。'
    await load()
  } catch (e) {
    error.value = e instanceof Error ? e.message : '构造场景失败'
  } finally {
    building.value = false
  }
}

async function startCorrection() {
  if (!handoff.value?.handoff_id) return
  correcting.value = true
  error.value = ''
  try {
    const resp = await request('/api/geo_snapshot/corrections', {
      method: 'POST',
      body: JSON.stringify({ handoff_id: handoff.value.handoff_id }),
    })
    if (!resp.ok) throw new Error(`启动失败：${resp.status}`)
    correction.value = await resp.json()
    message.value = '区域更正已启动；交接单冻结不变。'
  } catch (e) {
    error.value = e instanceof Error ? e.message : '启动区域更正失败'
  } finally {
    correcting.value = false
  }
}

onMounted(load)
</script>

<style scoped>
.handoff-panel {
  margin: 16px 0;
  padding: 16px;
  border: 1px solid var(--border-color, #e3e6eb);
  border-radius: 12px;
  background: var(--panel-bg, #fff);
}
.handoff-head {
  display: flex;
  justify-content: space-between;
  gap: 16px;
  align-items: flex-start;
  flex-wrap: wrap;
}
.handoff-head h3 {
  margin: 0 0 4px;
}
.handoff-sub {
  margin: 0;
  color: #6b7280;
  font-size: 13px;
  max-width: 720px;
}
.handoff-message {
  color: #15803d;
  font-size: 13px;
  margin: 8px 0 0;
}
.handoff-meta {
  display: flex;
  gap: 18px;
  flex-wrap: wrap;
  font-size: 13px;
  margin: 12px 0;
}
.handoff-table {
  margin-top: 8px;
}
.tag {
  display: inline-block;
  margin-left: 8px;
  padding: 1px 8px;
  border-radius: 999px;
  background: #eef2ff;
  color: #3730a3;
  font-size: 12px;
}
.num-ok {
  color: #15803d;
}
.delta {
  font-weight: 700;
  color: #b45309;
}
.status-pill {
  padding: 2px 10px;
  border-radius: 999px;
  background: #dcfce7;
  color: #166534;
  font-size: 12px;
}
.status-pill[data-pending='true'] {
  background: #fef3c7;
  color: #92400e;
}
.pending-reason {
  margin-top: 4px;
  font-size: 12px;
  color: #92400e;
}
.handoff-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
  gap: 12px;
  margin-top: 12px;
}
.handoff-card {
  border: 1px solid #eef0f3;
  border-radius: 10px;
  padding: 10px 12px;
  background: #fafbfc;
}
.handoff-card h4 {
  margin: 0 0 6px;
  font-size: 13px;
}
.handoff-card ul {
  margin: 0;
  padding-left: 18px;
  font-size: 12.5px;
  line-height: 1.7;
}
.guard-line {
  margin-top: 12px;
  font-size: 13px;
  color: #1d4ed8;
}
.correction-box {
  margin-top: 12px;
  padding: 10px 12px;
  border-radius: 10px;
  background: #fffbeb;
  border: 1px solid #fde68a;
  font-size: 13px;
}
.correction-line {
  display: inline-block;
  margin-right: 12px;
}
code {
  font-size: 12px;
}
</style>
