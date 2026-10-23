<script setup>
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import zhifuMark from './assets/zhifu.svg'
import userProfileAvatar from './assets/userProfileAvatar.svg'

const senderId = ref('c1001')
const draftMessage = ref('')
const isSending = ref(false)
const errorMessage = ref('')
const messages = ref([])
const messagesContainer = ref(null)

const shipments = ref([])
const isLoadingSidebar = ref(false)
const sidebarError = ref('')

// TTS state
const ttsState = ref({})
let currentAudio = null

// Copy state
const copyState = ref({})

// 客服数字人配置
const customerService = {
  name: '履约客服助手助手',
  title: '仓配履约智能客服',
  avatar: zhifuMark,
  status: '在线'
}

// 用户配置
const userProfile = {
  name: '你',
  avatar: userProfileAvatar
}

// 将消息分组为 Turn 结构
const turns = computed(() => {
  const result = []
  let currentTurn = null
  let turnIndex = 0

  for (const message of messages.value) {
    if (message.type === 'divider') {
      if (currentTurn) {
        result.push(currentTurn)
        currentTurn = null
      }
      result.push({
        type: 'divider',
        text: message.text
      })
      continue
    }

    if (message.role === 'user') {
      // 如果有待处理的当前 turn，先保存
      if (currentTurn) {
        result.push(currentTurn)
      }
      // 创建新的 turn
      turnIndex++
      currentTurn = {
        type: 'turn',
        id: `turn-${turnIndex}`,
        index: turnIndex,
        userMessage: message,
        botMessages: []
      }
    } else if (message.role === 'bot') {
      if (!currentTurn) {
        // 如果没有当前 turn，创建一个（可能是因为历史消息）
        turnIndex++
        currentTurn = {
          type: 'turn',
          id: `turn-${turnIndex}`,
          index: turnIndex,
          userMessage: null,
          botMessages: []
        }
      }
      currentTurn.botMessages.push(message)
    }
  }

  if (currentTurn) {
    result.push(currentTurn)
  }

  return result
})

const chatEndpoint = computed(() => '/api/chat')
const chatHistoryEndpoint = computed(
  () => `/api/chat/history?sender_id=${encodeURIComponent(senderId.value.trim())}`
)
const commerceShipmentsEndpoint = computed(
  () => `/commerce/customers/${encodeURIComponent(senderId.value.trim())}/shipments`
)

function createBaseMessage(role) {
  return {
    id: crypto.randomUUID(),
    role,
    buttons: [],
  }
}

function appendUserText(text) {
  messages.value.push({
    ...createBaseMessage('user'),
    type: 'text',
    text,
  })
}

function appendUserObject(objectType, payload) {
  messages.value.push({
    ...createBaseMessage('user'),
    type: 'object',
    objectType,
    payload,
  })
}

function appendBotMessages(botMessages) {
  for (const message of botMessages) {
    appendMessage('bot', message)
  }
}

function appendMessage(role, message) {
  if (role === 'divider') {
    messages.value.push({
      ...createBaseMessage('divider'),
      type: 'divider',
      text: message.text ?? '以上为历史消息',
    })
    return
  }

  if (message.object) {
    messages.value.push({
      ...createBaseMessage(role),
      type: 'object',
      objectType: message.object.type,
      payload: message.object,
    })
  } else {
    messages.value.push({
      ...createBaseMessage(role),
      type: 'text',
      text: message.text ?? '',
      suggestions: message.suggestions ?? null,
    })
  }
}

function setHistoryMessages(historyMessages) {
  messages.value = []
  for (const message of historyMessages) {
    const role = ['user', 'bot', 'divider'].includes(message.role) ? message.role : 'bot'
    appendMessage(role, message)
  }
}

async function scrollToBottom() {
  await nextTick()
  const container = messagesContainer.value
  if (!container) {
    return
  }
  container.scrollTop = container.scrollHeight
}

watch(
  () => messages.value.length,
  async () => {
    await scrollToBottom()
  }
)

function resetConversation() {
  messages.value = []
  errorMessage.value = ''
}

function formatAmount(amount) {
  const numericAmount = Number(amount)
  if (Number.isNaN(numericAmount)) {
    return '￥0.00'
  }
  return `￥${numericAmount.toFixed(2)}`
}

function formatShipmentSummary(shipment) {
  if (!shipment) {
    return '运单'
  }
  const parts = [shipment.goods_desc, shipment.status].filter(Boolean)
  return parts.length ? parts.join(' · ') : '运单'
}

const SHIPMENT_STATUS_CLASS = {
  '已下单': 'status-muted',
  '仓库备货': 'status-warning',
  '已出库': 'status-info',
  '干线运输中': 'status-info',
  '到达派送网点': 'status-info',
  '派送中': 'status-info',
  '已妥投': 'status-success',
  '异常': 'status-danger',
}

function getStatusClass(status) {
  return SHIPMENT_STATUS_CLASS[status] || 'status-muted'
}

function getObjectTitle(message) {
  const payload = message.payload ?? {}
  if (payload.title) {
    return payload.title
  }
  return '运单对象'
}

function getObjectIdentifier(message) {
  const payload = message.payload ?? {}
  const id = payload.tracking_number ?? payload.id
  return id ? `运单号：${id}` : '运单号'
}

function getObjectSummary(message) {
  const payload = message.payload ?? {}
  const status = payload.status ?? payload.attributes?.status
  return status ? `运单状态：${status}` : '运单'
}

function getObjectAmount(message) {
  const payload = message.payload ?? {}
  const amount = payload.freight ?? payload.attributes?.freight
  if (amount === null || amount === undefined || amount === '') {
    return ''
  }
  const numericAmount = Number(amount)
  if (Number.isNaN(numericAmount)) {
    return ''
  }
  return `运费 ￥${numericAmount.toFixed(2)}`
}

async function fetchSidebarData() {
  const currentSenderId = senderId.value.trim()
  shipments.value = []
  sidebarError.value = ''

  if (!currentSenderId) {
    return
  }

  isLoadingSidebar.value = true
  try {
    const shipmentsResponse = await fetch(commerceShipmentsEndpoint.value)
    const shipmentsPayload = await shipmentsResponse.json()

    if (!shipmentsResponse.ok) {
      throw new Error(shipmentsPayload.detail || '加载运单列表失败。')
    }

    shipments.value = Array.isArray(shipmentsPayload?.data?.shipments)
      ? shipmentsPayload.data.shipments
      : []
  } catch (error) {
    sidebarError.value = error instanceof Error ? error.message : '加载右侧列表失败。'
  } finally {
    isLoadingSidebar.value = false
  }
}

async function fetchChatHistory() {
  const currentSenderId = senderId.value.trim()
  if (!currentSenderId) {
    messages.value = []
    return
  }

  try {
    const response = await fetch(chatHistoryEndpoint.value)
    const data = await response.json()
    if (!response.ok) {
      throw new Error(data.detail || '加载历史消息失败。')
    }
    if (currentSenderId === senderId.value.trim()) {
      setHistoryMessages(Array.isArray(data?.messages) ? data.messages : [])
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '加载历史消息失败。'
  }
}

async function sendPayload(payload) {
  if (isSending.value) {
    return
  }

  errorMessage.value = ''
  isSending.value = true

  try {
    const response = await fetch(chatEndpoint.value, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({
        sender_id: senderId.value.trim(),
        ...payload,
      }),
    })

    const data = await response.json()
    if (!response.ok) {
      throw new Error(data.detail || '请求失败。')
    }

    appendBotMessages(data.messages ?? [])
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '请求失败。'
  } finally {
    isSending.value = false
  }
}

async function sendSuggestion(text) {
  appendUserText(text)
  await sendPayload({ text })
}

async function sendQuickText(text) {
  draftMessage.value = text
  await sendTextMessage()
}
async function sendTextMessage() {
  const text = draftMessage.value.trim()
  const currentSenderId = senderId.value.trim()

  if (!currentSenderId) {
    errorMessage.value = '请先输入 sender_id。'
    return
  }
  if (!text) {
    return
  }

  draftMessage.value = ''
  appendUserText(text)
  await sendPayload({ text })
}

async function sendShipment(shipment) {
  const currentSenderId = senderId.value.trim()
  if (!currentSenderId) {
    errorMessage.value = '请先输入 sender_id。'
    return
  }

  appendUserObject('shipment', { ...shipment })
  await sendPayload({
    object: {
      type: 'shipment',
      id: shipment.tracking_number,
      title: shipment.goods_desc,
      attributes: {
        tracking_number: shipment.tracking_number,
        status: shipment.status,
        carrier: shipment.carrier,
        promise_time: shipment.promise_time,
        created_at: shipment.created_at,
      },
    },
  })
}

watch(
  () => senderId.value.trim(),
  async (value, previousValue) => {
    if (value === previousValue) {
      return
    }

    resetConversation()
    if (!value) {
      shipments.value = []
      return
    }
    await Promise.all([fetchSidebarData(), fetchChatHistory()])
  }
)

onMounted(async () => {
  await Promise.all([fetchSidebarData(), fetchChatHistory()])
})

async function playTts(botMsg) {
  const msgId = botMsg.id
  if (!botMsg.text || ttsState.value[msgId] === 'loading') return

  if (currentAudio) { currentAudio.pause(); currentAudio = null }
  for (const key of Object.keys(ttsState.value)) {
    if (ttsState.value[key] === 'playing') ttsState.value[key] = 'idle'
  }

  ttsState.value[msgId] = 'loading'
  try {
    const response = await fetch('/api/chat/tts', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text: botMsg.text }),
    })
    if (!response.ok) throw new Error('TTS 请求失败')
    const blob = await response.blob()
    const url = URL.createObjectURL(blob)
    currentAudio = new Audio(url)
    ttsState.value[msgId] = 'playing'
    currentAudio.onended = () => { ttsState.value[msgId] = 'idle'; URL.revokeObjectURL(url); currentAudio = null }
    currentAudio.onerror = () => { ttsState.value[msgId] = 'idle'; URL.revokeObjectURL(url); currentAudio = null }
    await currentAudio.play()
  } catch (error) {
    ttsState.value[msgId] = 'idle'
    console.error('TTS error:', error)
  }
}

async function copyBotText(botMsg) {
  const msgId = botMsg.id
  try {
    await navigator.clipboard.writeText(botMsg.text)
    copyState.value[msgId] = true
    setTimeout(() => { copyState.value[msgId] = false }, 1800)
  } catch (error) {
    console.error('Copy failed:', error)
  }
}
</script>

<template>
  <div class="app-shell">
    <div class="workspace">
      <div class="chat-card">
        <header class="chat-header">
          <div class="header-content">
            <div class="header-info">
              <h1>履约客服助手</h1>
              <div class="service-info">
                <div class="service-avatar-wrapper">
                  <img :src="customerService.avatar" class="service-avatar" />
                  <span class="status-indicator"></span>
                </div>
                <div class="service-details">
                  <span class="service-name">{{ customerService.name }}</span>
                  <span class="service-status">{{ customerService.status }}</span>
                </div>
              </div>
            </div>
            <button type="button" class="clear-button" title="清空当前会话" @click="resetConversation">
              <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">
                <path d="M13.5 8a5.5 5.5 0 1 1-1.61-3.89M13.5 2v3h-3"
                      fill="none" stroke="currentColor" stroke-width="1.5"
                      stroke-linecap="round" stroke-linejoin="round"/>
              </svg>
              <span>新会话</span>
            </button>
          </div>
        </header>

        <section class="controls">
          <label class="field">
            <span>sender_id</span>
            <div class="field-row">
              <input v-model="senderId" type="text" placeholder="c1001" />
              <button
                type="button"
                class="secondary-button"
                :disabled="isLoadingSidebar"
                @click="fetchSidebarData"
              >
                {{ isLoadingSidebar ? '加载中...' : '刷新对象列表' }}
              </button>
            </div>
          </label>
        </section>

        <section ref="messagesContainer" class="messages">
          <div v-if="turns.length === 0" class="welcome">
            <div class="welcome-card">
              <div class="welcome-brand">
                <img :src="customerService.avatar" class="welcome-avatar" alt="履约客服助手助手" />
              </div>
              <h2 class="welcome-greeting">履约客服助手 · 仓配履约客服</h2>
              <p class="welcome-subtitle">
                查询运单状态与轨迹、修改派送地址、催办派送、提交异常件理赔。
                多轮对话中途插话或改变主意，任务状态会保留，可从断点继续。
              </p>
              <div class="welcome-chips">
                <button
                  v-for="chip in ['查一下 YD20260408002 到哪了', '我要报异常', '破损件怎么赔', '转人工']"
                  :key="chip"
                  type="button"
                  class="welcome-chip"
                  :disabled="isSending"
                  @click="sendQuickText(chip)"
                >{{ chip }}</button>
              </div>
              <dl class="welcome-specs">
                <div><dt>对话引擎</dt><dd>规划 · 校验 · 调度 · 推进</dd></div>
                <div><dt>业务流程</dt><dd>7 条</dd></div>
                <div><dt>防幻觉</dt><dd>四层校验</dd></div>
              </dl>
            </div>
          </div>

          <!-- Turn 结构展示 -->
          <template v-for="(item, index) in turns" :key="item.id || index">
            <!-- 分隔线 -->
            <div v-if="item.type === 'divider'" class="history-divider">
              <span>{{ item.text }}</span>
            </div>

            <!-- Turn 卡片 -->
            <div v-else class="turn-card" :class="{ 'has-user-message': item.userMessage }">
              <!-- Turn 标识 -->
              <div class="turn-header">
                <span class="turn-badge">Turn {{ item.index }}</span>
                <span class="turn-label">对话轮次</span>
              </div>

              <!-- 用户消息区域 -->
              <div v-if="item.userMessage" class="turn-section user-section">
                <div class="section-header">
                  <div class="avatar-wrapper user-avatar">
                    <img :src="userProfile.avatar" class="avatar" />
                  </div>
                  <div class="agent-info">
                    <span class="agent-name">{{ userProfile.name }}</span>
                    <span class="agent-label">用户</span>
                  </div>
                </div>
                <div class="turn-bubble user-bubble">
                  <template v-if="item.userMessage.type === 'object'">
                    <div class="object-card" :class="`object-card-${item.userMessage.objectType}`">
                      <div class="object-card-badge">运单</div>
                      <img
                        v-if="item.userMessage.type === 'object' && item.userMessage.payload.cover_url"
                        :src="item.userMessage.payload.cover_url"
                        :alt="getObjectTitle(item.userMessage)"
                        class="object-card-image"
                        @error="$event.target.style.display='none'"
                      />
                      <div class="object-card-title">{{ getObjectTitle(item.userMessage) }}</div>
                      <div class="object-card-meta">{{ getObjectIdentifier(item.userMessage) }}</div>
                      <div class="object-card-meta">
                        <span v-if="item.userMessage.objectType === 'shipment' && item.userMessage.payload.status" class="status-badge" :class="getStatusClass(item.userMessage.payload.status)">{{ item.userMessage.payload.status }}</span>
                        <span v-else>{{ getObjectSummary(item.userMessage) }}</span>
                      </div>
                      <div class="object-card-price">{{ getObjectAmount(item.userMessage) }}</div>
                    </div>
                  </template>
                  <template v-else>
                    <p>{{ item.userMessage.text }}</p>
                  </template>
                </div>
              </div>

              <!-- 客服回复区域 -->
              <div v-if="item.botMessages.length > 0" class="turn-section bot-section">
                <div class="section-header">
                  <div class="avatar-wrapper service-avatar">
                    <img :src="customerService.avatar" class="avatar" />
                    <span class="status-dot"></span>
                  </div>
                  <div class="agent-info">
                    <span class="agent-name">{{ customerService.name }}</span>
                    <span class="agent-label">{{ customerService.title }}</span>
                  </div>
                </div>
                <div class="bot-messages">
                  <div
                    v-for="(botMsg, msgIndex) in item.botMessages"
                    :key="msgIndex"
                    class="turn-bubble bot-bubble"
                  >
                    <template v-if="botMsg.type === 'object'">
                      <div class="object-card" :class="`object-card-${botMsg.objectType}`">
                        <div class="object-card-badge">运单</div>
                        <img
                          v-if="botMsg.type === 'object' && botMsg.payload.cover_url"
                          :src="botMsg.payload.cover_url"
                          :alt="getObjectTitle(botMsg)"
                          class="object-card-image"
                          @error="$event.target.style.display='none'"
                        />
                        <div class="object-card-title">{{ getObjectTitle(botMsg) }}</div>
                        <div class="object-card-meta">{{ getObjectIdentifier(botMsg) }}</div>
                        <div class="object-card-meta">
                          <span v-if="botMsg.objectType === 'shipment' && botMsg.payload.status" class="status-badge" :class="getStatusClass(botMsg.payload.status)">{{ botMsg.payload.status }}</span>
                          <span v-else>{{ getObjectSummary(botMsg) }}</span>
                        </div>
                        <div class="object-card-price">{{ getObjectAmount(botMsg) }}</div>
                      </div>
                    </template>
                    <template v-else>
                      <div class="bot-text-row">
                        <p>{{ botMsg.text }}</p>
                        <div class="bot-actions">
                          <button type="button" class="tts-button"
                            :class="{ 'tts-loading': ttsState[botMsg.id] === 'loading', 'tts-playing': ttsState[botMsg.id] === 'playing' }"
                            :disabled="ttsState[botMsg.id] === 'loading'"
                            :title="ttsState[botMsg.id] === 'playing' ? '正在播放...' : '语音播报'"
                            @click.stop="playTts(botMsg)">
                            <span v-if="ttsState[botMsg.id] === 'loading'" class="tts-spinner"></span>
                            <span v-else-if="ttsState[botMsg.id] === 'playing'" class="tts-bars">
                              <span></span><span></span><span></span>
                            </span>
                            <span v-else>🔈</span>
                          </button>
                          <button type="button" class="copy-button"
                            :class="{ 'copy-done': copyState[botMsg.id] }"
                            :title="copyState[botMsg.id] ? '已复制' : '复制文字'"
                            @click.stop="copyBotText(botMsg)">
                            <span v-if="copyState[botMsg.id]">✓</span>
                            <span v-else>📋</span>
                          </button>
                        </div>
                      </div>
                    </template>
                    <div v-if="botMsg.suggestions && botMsg.suggestions.length > 0" class="suggestion-chips">
                      <button
                        v-for="sug in botMsg.suggestions"
                        :key="sug"
                        type="button"
                        class="suggestion-chip"
                        :disabled="isSending"
                        @click.stop="sendSuggestion(sug)"
                      >{{ sug }}</button>
                    </div>
                  </div>
                </div>
              </div>
            </div>
          </template>
        </section>

        <div v-if="isSending" class="typing-indicator">
          <div class="typing-avatar">
            <img :src="customerService.avatar" class="avatar-small" alt="履约客服助手助手" />
          </div>
          <div class="typing-bubble">
            <span class="typing-dots">
              <span></span><span></span><span></span>
            </span>
            <span class="typing-label">小谷正在输入...</span>
          </div>
        </div>

        <p v-if="errorMessage" class="error-message">
          {{ errorMessage }}
        </p>

        <form class="composer" @submit.prevent="sendTextMessage">
          <input
            v-model="draftMessage"
            type="text"
            placeholder="请输入咨询内容..."
            :disabled="isSending"
          />
          <button type="submit" :disabled="isSending || !draftMessage.trim()">
            {{ isSending ? '发送中...' : '发送' }}
          </button>
        </form>
      </div>

      <aside class="sidebar">
        <div class="sidebar-header">
          <h2>我的运单</h2>
        </div>

        <p v-if="sidebarError" class="sidebar-error">{{ sidebarError }}</p>

        <div class="sidebar-list">
          <div v-if="!shipments.length && !isLoadingSidebar" class="sidebar-empty">
            暂无运单数据
          </div>

          <article v-for="shipment in shipments" :key="shipment.tracking_number" class="sidebar-card">
            <div class="card-top">
              <div class="card-title">{{ shipment.goods_desc }}</div>
              <div class="card-amount">{{ shipment.carrier }}</div>
            </div>
            <div class="card-meta">运单号：{{ shipment.tracking_number }}</div>
            <div class="card-meta">时效承诺：{{ shipment.promise_time }}</div>
            <div class="card-meta">
              <span class="status-badge" :class="getStatusClass(shipment.status)">{{ shipment.status }}</span>
            </div>
            <button
              type="button"
              class="secondary-button full-width"
              :disabled="isSending"
              @click="sendShipment(shipment)"
            >
              发送运单
            </button>
          </article>
        </div>
      </aside>
    </div>
  </div>
</template>

<style scoped>
:global(*) {
  box-sizing: border-box;
}

:global(:root) {
  /* ── 字体 ────────────────────────────────────────────────
     中文走系统黑体（苹方 / 微软雅黑 / 思源黑体），不用装饰性网络字体：
     一是离线也能正确渲染，二是 B2B 控制台不需要圆润的展示型字形。 */
  --font-sans: system-ui, -apple-system, "Segoe UI", "PingFang SC", "Hiragino Sans GB",
               "Microsoft YaHei", "Source Han Sans SC", "Noto Sans CJK SC",
               "Helvetica Neue", Arial, sans-serif;
  --font-mono: ui-monospace, SFMono-Regular, "SF Mono", "Cascadia Mono",
               Consolas, "Liberation Mono", monospace;

  /* ── 中性底 ── */
  --color-bg-deep: #f2f4f6;
  --color-bg-mid: #eceff2;
  --color-bg-surface: #ffffff;
  /* ── 表面 ── */
  --color-surface: #ffffff;
  --color-surface-hover: #fcfcfd;
  --color-surface-dim: #f8f9fa;
  --color-surface-raised: #ffffff;
  --color-surface-field: #f7f8fa;
  /* ── 文字 ── */
  --color-text-primary: #14181f;
  --color-text-secondary: #4b5361;
  --color-text-muted: #7c8593;
  --color-text-inverse: #ffffff;
  /* ── 主色：钢蓝。单一强调色，不铺渐变 ── */
  --color-accent: #14487f;
  --color-accent-strong: #0f3a69;
  --color-accent-soft: rgba(20, 72, 127, 0.08);
  --color-accent-glow: rgba(20, 72, 127, 0.10);
  --color-accent-soft-bg: rgba(20, 72, 127, 0.04);
  /* ── 副色：板岩灰（用于数值、次要强调） ── */
  --color-warm: #5a6472;
  --color-warm-strong: #3f4854;
  --color-warm-soft: rgba(90, 100, 114, 0.10);
  --color-warm-glow: rgba(90, 100, 114, 0.10);
  --color-warm-soft-bg: rgba(90, 100, 114, 0.05);
  /* ── 语义色（降饱和，保证在白底上稳） ── */
  --color-success: #16704a;
  --color-success-soft: rgba(22, 112, 74, 0.09);
  --color-info: #17548f;
  --color-info-soft: rgba(23, 84, 143, 0.09);
  --color-warning: #8a5a10;
  --color-warning-soft: rgba(138, 90, 16, 0.10);
  --color-danger: #a32a3c;
  --color-danger-soft: rgba(163, 42, 60, 0.09);
  /* ── 描边：实色细线，比半透明更清晰 ── */
  --color-border: #e0e4e9;
  --color-border-light: #eaedf1;
  --color-border-strong: #c9cfd8;
  /* ── 圆角：小圆角，硬朗 ── */
  --radius-sm: 3px;
  --radius-md: 5px;
  --radius-lg: 7px;
  --radius-xl: 9px;
  --radius-full: 3px;
  /* ── 阴影：真实投影，不用彩色光晕 ── */
  --shadow-xs: 0 1px 1px rgba(16, 24, 40, 0.05);
  --shadow-sm: 0 1px 3px rgba(16, 24, 40, 0.08);
  --shadow-md: 0 4px 12px rgba(16, 24, 40, 0.09);
  --shadow-lg: 0 12px 32px rgba(16, 24, 40, 0.10);
  --shadow-glow-teal: var(--shadow-md);
  --shadow-glow-amber: var(--shadow-md);
  --shadow-inner-glow: none;
  /* ── 动效：只保留必要的，去掉回弹 ── */
  --ease-out-expo: cubic-bezier(0.16, 1, 0.3, 1);
  --ease-out-back: cubic-bezier(0.16, 1, 0.3, 1);
  --ease-in-out: cubic-bezier(0.4, 0, 0.2, 1);
  --duration-fast: 120ms;
  --duration-base: 180ms;
  --duration-slow: 260ms;
}

:global(body) {
  margin: 0;
  font-family: var(--font-sans);
  font-size: 14px;
  line-height: 1.6;
  background: var(--color-bg-deep);
  color: var(--color-text-primary);
  -webkit-font-smoothing: antialiased;
  -moz-osx-font-smoothing: grayscale;
}

:global(button),
:global(input) {
  font: inherit;
}

:global(#app) {
  min-height: 100vh;
}

.app-shell {
  min-height: 100vh;
  padding: 20px;
  background: var(--color-bg-deep);
}

.workspace {
  width: min(1560px, 100%);
  margin: 0 auto;
  display: grid;
  grid-template-columns: minmax(0, 1fr) 340px;
  gap: 16px;
}

.chat-card,
.sidebar {
  min-height: calc(100vh - 40px);
  height: calc(100vh - 40px);
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
  overflow: hidden;
}
.chat-card {
  display: flex;
  flex-direction: column;
  box-shadow: var(--shadow-md);
  position: relative;
}
/* 顶部主色压条：给界面一个稳定的视觉落点 */
.chat-card::before {
  content: "";
  position: absolute;
  top: 0; left: 0; right: 0;
  height: 2px;
  background: var(--color-accent);
  z-index: 2;
}
.sidebar {
  display: flex;
  flex-direction: column;
  box-shadow: var(--shadow-sm);
}

.chat-header,
.sidebar-header {
  padding: 16px 20px;
  border-bottom: 1px solid var(--color-border);
  background: var(--color-surface);
}

.chat-header {
  display: flex;
  justify-content: space-between;
  gap: 16px;
  align-items: center;
}

.header-content {
  display: flex;
  align-items: center;
  gap: 14px;
  width: 100%;
}

.header-info {
  display: flex;
  flex-direction: column;
  gap: 6px;
  flex: 1;
  min-width: 0;
}

.chat-header h1,
.sidebar-header h2 {
  margin: 0;
  font-size: 16px;
  line-height: 1.3;
  font-weight: 600;
  color: var(--color-text-primary);
  letter-spacing: 0.01em;
}

.sidebar-header h2 {
  font-size: 14px;
  color: var(--color-text-secondary);
  font-weight: 600;
  text-transform: none;
}

.chat-header p,
.sidebar-header p {
  margin: 6px 0 0;
  font-size: 13px;
  color: var(--color-text-secondary);
}

/* 客服信息 */
.service-info {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 8px 16px;
  background: var(--color-surface-raised);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
}

.service-avatar-wrapper {
  position: relative;
  flex-shrink: 0;
}

.service-avatar {
  width: 38px;
  height: 38px;
  border-radius: var(--radius-md);
  object-fit: contain;
  padding: 4px;
  background: var(--color-surface-field);
  border: 1px solid var(--color-border);
}

.status-indicator {
  position: absolute;
  bottom: -2px;
  right: -2px;
  width: 9px;
  height: 9px;
  background: var(--color-success);
  border: 2px solid var(--color-surface);
  border-radius: 50%;
}

.service-details {
  display: flex;
  flex-direction: column;
  gap: 1px;
  min-width: 0;
}

.service-name {
  font-size: 13px;
  font-weight: 600;
  color: var(--color-text-primary);
}

.service-status {
  font-size: 12px;
  color: var(--color-text-muted);
  font-weight: 400;
  display: flex;
  align-items: center;
  gap: 4px;
}

.service-status::before {
  content: '';
  width: 5px;
  height: 5px;
  background: var(--color-success);
  border-radius: 50%;
}

.clear-button {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 6px 12px;
  border: 1px solid var(--color-border-strong);
  border-radius: var(--radius-md);
  background: var(--color-surface);
  color: var(--color-text-secondary);
  font-size: 13px;
  font-weight: 500;
  cursor: pointer;
  transition: background var(--duration-fast) var(--ease-in-out),
              border-color var(--duration-fast) var(--ease-in-out),
              color var(--duration-fast) var(--ease-in-out);
  white-space: nowrap;
}
.clear-button:hover {
  background: var(--color-danger-soft);
  border-color: var(--color-danger);
  color: var(--color-danger);
}

.controls {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 10px 20px;
  border-bottom: 1px solid var(--color-border);
  background: var(--color-surface-dim);
}

.field {
  display: flex;
  align-items: center;
  gap: 8px;
  flex: 1;
  min-width: 0;
}

.field span {
  color: var(--color-text-secondary);
  font-size: 12px;
  font-weight: 500;
  white-space: nowrap;
}

.field-row {
  display: flex;
  gap: 8px;
  flex: 1;
  min-width: 0;
}

.field input,
.composer input {
  width: 100%;
  min-width: 0;
  min-height: 34px;
  padding: 6px 10px;
  border: 1px solid var(--color-border-strong);
  border-radius: var(--radius-md);
  background: var(--color-surface);
  color: var(--color-text-primary);
  font-size: 13px;
  font-family: var(--font-mono);
  line-height: 1.4;
  transition: border-color var(--duration-fast) var(--ease-in-out), box-shadow var(--duration-fast) var(--ease-in-out);
}
.field input::placeholder,
.composer input::placeholder {
  color: var(--color-text-muted);
  font-family: var(--font-sans);
}
.field input:focus,
.composer input:focus {
  outline: none;
  border-color: var(--color-accent);
  box-shadow: 0 0 0 2px var(--color-accent-soft);
}

.messages {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  scrollbar-gutter: stable;
  padding: 20px;
  display: flex;
  flex-direction: column;
  gap: 16px;
}

/* Turn 卡片样式 */
.turn-card {
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding: 16px 18px;
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
  transition: border-color var(--duration-base) var(--ease-in-out);
  animation: msgEnter 0.2s var(--ease-out-expo) both;
}
@keyframes msgEnter {
  from { opacity: 0; transform: translateY(6px); }
  to { opacity: 1; transform: translateY(0); }
}
.turn-card:hover {
  border-color: var(--color-border-strong);
}

.turn-header {
  display: flex;
  align-items: center;
  gap: 8px;
  padding-bottom: 10px;
  border-bottom: 1px solid var(--color-border-light);
}

.turn-badge {
  display: inline-flex;
  align-items: center;
  padding: 2px 7px;
  background: var(--color-accent);
  color: #ffffff;
  font-size: 11px;
  font-weight: 600;
  font-family: var(--font-mono);
  letter-spacing: 0.03em;
  border-radius: var(--radius-sm);
}

.turn-label {
  font-size: 11px;
  color: var(--color-text-muted);
  font-weight: 400;
  letter-spacing: 0.04em;
}

.turn-section {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.section-header {
  display: flex;
  align-items: center;
  gap: 12px;
  font-size: 13px;
  font-weight: 600;
}

/* 头像样式 */
.avatar-wrapper {
  position: relative;
  flex-shrink: 0;
}

.avatar {
  width: 30px;
  height: 30px;
  border-radius: var(--radius-md);
  object-fit: contain;
  border: 1px solid var(--color-border);
  background: var(--color-surface-field);
  padding: 3px;
}

.status-dot {
  position: absolute;
  bottom: -1px;
  right: -1px;
  width: 8px;
  height: 8px;
  background: var(--color-success);
  border: 2px solid var(--color-surface);
  border-radius: 50%;
}

.agent-info {
  display: flex;
  flex-direction: column;
  gap: 0;
}

.agent-name {
  font-size: 13px;
  font-weight: 600;
  color: var(--color-text-primary);
  line-height: 1.4;
}

.agent-label {
  font-size: 11px;
  color: var(--color-text-muted);
  font-weight: 400;
  line-height: 1.4;
}

.role-icon {
  font-size: 14px;
}

.role-label {
  color: var(--color-text-secondary);
}

.turn-bubble {
  padding: 10px 14px;
  border-radius: var(--radius-md);
  max-width: 100%;
}

.user-bubble {
  background: var(--color-accent);
  border: 1px solid var(--color-accent);
  color: #ffffff;
  margin-left: auto;
  max-width: 85%;
}

.bot-bubble {
  background: var(--color-surface-field);
  border: 1px solid var(--color-border);
  color: var(--color-text-primary);
}

.bot-messages {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.turn-bubble p {
  margin: 0;
  font-size: 14px;
  line-height: 1.7;
  white-space: pre-wrap;
  word-break: break-word;
}

.user-bubble p {
  color: #ffffff;
}

/* 历史消息分隔线 */
.history-divider {
  flex-shrink: 0;
  display: flex;
  align-items: center;
  gap: 12px;
  color: var(--color-text-muted);
  font-size: 12px;
  padding: 4px 0;
}

.history-divider::before,
.history-divider::after {
  content: "";
  flex: 1;
  height: 1px;
  background: var(--color-border);
}

.history-divider span {
  padding: 2px 10px;
  border-radius: var(--radius-sm);
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  font-size: 11px;
  letter-spacing: 0.04em;
}

.welcome {
  flex-shrink: 0;
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
}
.welcome-card {
  max-width: 560px;
  width: 100%;
  padding: 32px 32px 28px;
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-sm);
  animation: msgEnter 0.24s var(--ease-out-expo);
}
.welcome-brand {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 44px;
  height: 44px;
  margin-bottom: 16px;
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  background: var(--color-surface-field);
}
.welcome-avatar {
  width: 28px;
  height: 28px;
  object-fit: contain;
}
.welcome-greeting {
  margin: 0 0 10px;
  font-size: 19px;
  font-weight: 600;
  color: var(--color-text-primary);
  letter-spacing: 0.01em;
}
.welcome-subtitle {
  margin: 0 0 20px;
  color: var(--color-text-secondary);
  font-size: 13px;
  line-height: 1.75;
  max-width: 46em;
}
.welcome-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 22px;
}
.welcome-chip {
  padding: 6px 12px;
  border: 1px solid var(--color-border-strong);
  border-radius: var(--radius-md);
  background: var(--color-surface);
  color: var(--color-text-secondary);
  font-size: 13px;
  font-weight: 400;
  cursor: pointer;
  transition: background var(--duration-fast) var(--ease-in-out),
              border-color var(--duration-fast) var(--ease-in-out),
              color var(--duration-fast) var(--ease-in-out);
}
.welcome-chip:hover:not(:disabled) {
  background: var(--color-accent-soft-bg);
  border-color: var(--color-accent);
  color: var(--color-accent);
}
.welcome-chip:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
/* 能力说明：用表格而不是 emoji 列表 */
.welcome-specs {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 0;
  margin: 0;
  padding-top: 18px;
  border-top: 1px solid var(--color-border-light);
}
.welcome-specs > div {
  padding-right: 16px;
}
.welcome-specs > div + div {
  padding-left: 16px;
  border-left: 1px solid var(--color-border-light);
}
.welcome-specs dt {
  font-size: 11px;
  color: var(--color-text-muted);
  letter-spacing: 0.04em;
  margin-bottom: 3px;
}
.welcome-specs dd {
  margin: 0;
  font-size: 12px;
  color: var(--color-text-primary);
  font-weight: 500;
}

.sidebar-empty {
  margin: auto;
  max-width: 420px;
  color: var(--color-text-muted);
  text-align: center;
  line-height: 1.7;
}

.typing-indicator {
  flex-shrink: 0;
  display: flex;
  align-items: flex-end;
  gap: 10px;
  padding: 0 20px;
  animation: msgEnter 0.3s var(--ease-out-expo);
}
.avatar-small {
  width: 30px; height: 30px;
  border-radius: var(--radius-md);
  object-fit: contain;
  border: 1px solid var(--color-border);
  background: var(--color-surface-field);
  padding: 3px;
}
.typing-bubble {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 14px;
  background: var(--color-surface-field);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
}
.typing-dots {
  display: flex;
  gap: 3px;
  align-items: center;
}
.typing-dots span {
  width: 5px; height: 5px;
  border-radius: 50%;
  background: var(--color-text-muted);
  animation: dotBlink 1.2s ease-in-out infinite;
}
.typing-dots span:nth-child(1) { animation-delay: 0s; }
.typing-dots span:nth-child(2) { animation-delay: 0.2s; }
.typing-dots span:nth-child(3) { animation-delay: 0.4s; }
@keyframes dotBlink {
  0%, 60%, 100% { opacity: 0.25; }
  30% { opacity: 1; }
}
.typing-label {
  font-size: 12px;
  color: var(--color-text-muted);
}

.object-card-price {
  font-size: 13px;
  font-weight: 600;
  font-family: var(--font-mono);
}

/* ── 状态徽章 ────────────────────────────────────────────── */
.status-badge {
  display: inline-flex;
  align-items: center;
  padding: 2px 8px;
  border-radius: var(--radius-sm);
  font-size: 11px;
  font-weight: 500;
  letter-spacing: 0.02em;
  border: 1px solid transparent;
}
.status-warning {
  background: var(--color-warning-soft);
  color: var(--color-warning);
  border-color: rgba(138, 90, 16, 0.22);
}
.status-info {
  background: var(--color-info-soft);
  color: var(--color-info);
  border-color: rgba(23, 84, 143, 0.22);
}
.status-success {
  background: var(--color-success-soft);
  color: var(--color-success);
  border-color: rgba(22, 112, 74, 0.22);
}
.status-muted {
  background: var(--color-surface-field);
  color: var(--color-text-muted);
  border-color: var(--color-border);
}
.status-danger {
  background: var(--color-danger-soft);
  color: var(--color-danger);
  border-color: rgba(163, 42, 60, 0.22);
}

/* ── 运单对象卡片（聊天区） ────────────────────────────────── */
.object-card {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 10px 12px;
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  background: var(--color-surface);
  min-width: 200px;
}
.object-card-badge {
  align-self: flex-start;
  margin-bottom: 2px;
  padding: 1px 6px;
  border-radius: var(--radius-sm);
  background: var(--color-surface-field);
  border: 1px solid var(--color-border);
  color: var(--color-text-muted);
  font-size: 10px;
  font-weight: 500;
  letter-spacing: 0.06em;
}
.object-card-image {
  width: 100%;
  max-width: 120px;
  height: auto;
  object-fit: contain;
  border-radius: var(--radius-sm);
  margin-bottom: 4px;
  border: 1px solid var(--color-border-light);
  background: var(--color-surface-field);
}
.object-card-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--color-text-primary);
  line-height: 1.5;
}
.object-card-meta {
  font-size: 12px;
  color: var(--color-text-secondary);
  line-height: 1.6;
}
.object-card-meta:first-of-type {
  font-family: var(--font-mono);
  font-variant-numeric: tabular-nums;
  color: var(--color-text-primary);
}
.turn-bubble .object-card {
  min-width: 200px;
}

.user-bubble .object-card {
  background: rgba(255, 255, 255, 0.10);
  border-color: rgba(255, 255, 255, 0.32);
}
.user-bubble .object-card-title,
.user-bubble .object-card-meta,
.user-bubble .object-card-price {
  color: #ffffff;
}
.user-bubble .object-card-badge {
  background: rgba(255, 255, 255, 0.14);
  border-color: rgba(255, 255, 255, 0.30);
  color: rgba(255, 255, 255, 0.90);
}
.user-bubble .object-card-image {
  border-color: rgba(255, 255, 255, 0.25);
  background: rgba(255, 255, 255, 0.10);
}

.composer button,
.secondary-button {
  min-height: 34px;
  padding: 6px 14px;
  border: 1px solid var(--color-border-strong);
  border-radius: var(--radius-md);
  background: var(--color-surface);
  color: var(--color-text-primary);
  cursor: pointer;
  font-size: 13px;
  font-weight: 500;
  line-height: 1.2;
  white-space: nowrap;
  transition: background var(--duration-fast) var(--ease-in-out),
              border-color var(--duration-fast) var(--ease-in-out),
              color var(--duration-fast) var(--ease-in-out);
}

.composer button:hover:not(:disabled),
.secondary-button:hover:not(:disabled) {
  background: var(--color-surface-field);
  border-color: var(--color-text-muted);
}

.composer button:disabled,
.secondary-button:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.error-message,
.sidebar-error {
  margin: 0;
  padding: 0 20px 12px;
  color: var(--color-danger);
  font-size: 12px;
  font-weight: 500;
}

.composer {
  flex-shrink: 0;
  display: flex;
  align-items: stretch;
  gap: 8px;
  padding: 14px 20px;
  border-top: 1px solid var(--color-border);
  background: var(--color-surface-dim);
}

.composer input {
  font-family: var(--font-sans);
  font-size: 14px;
  min-height: 38px;
}

.composer button {
  min-width: 88px;
  background: var(--color-accent);
  border-color: var(--color-accent);
  color: #ffffff;
  font-weight: 500;
}
.composer button:hover:not(:disabled) {
  background: var(--color-accent-strong);
  border-color: var(--color-accent-strong);
}

.sidebar-list {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 12px 16px 20px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.sidebar-card {
  padding: 12px 14px;
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  background: var(--color-surface);
  display: flex;
  flex-direction: column;
  gap: 8px;
  transition: border-color var(--duration-fast) var(--ease-in-out),
              background var(--duration-fast) var(--ease-in-out);
}
.sidebar-card:hover {
  background: var(--color-surface-hover);
  border-color: var(--color-border-strong);
}

.card-top {
  display: flex;
  gap: 10px;
  justify-content: space-between;
  align-items: baseline;
}

.card-title {
  font-size: 13px;
  line-height: 1.5;
  color: var(--color-text-primary);
  font-weight: 600;
}

.card-amount {
  flex-shrink: 0;
  color: var(--color-text-secondary);
  font-size: 12px;
  font-weight: 500;
}

.card-meta {
  font-size: 12px;
  color: var(--color-text-secondary);
  line-height: 1.6;
}
/* 运单号：等宽 + 表格数字，避免 1/l/I 混淆 */
.card-meta:first-of-type {
  font-family: var(--font-mono);
  font-variant-numeric: tabular-nums;
  letter-spacing: 0.01em;
  color: var(--color-text-primary);
}

.bot-text-row { display: flex; align-items: flex-start; gap: 8px; }
.bot-text-row p { flex: 1; margin: 0; }
.bot-actions {
  display: flex; gap: 6px; flex-shrink: 0; align-items: center;
}
.tts-button, .copy-button {
  flex-shrink: 0; width: 26px; height: 26px; padding: 0;
  border: 1px solid var(--color-border); border-radius: var(--radius-sm);
  background: var(--color-surface); cursor: pointer;
  font-size: 12px; display: inline-flex; align-items: center; justify-content: center;
  transition: background var(--duration-fast) var(--ease-in-out),
              border-color var(--duration-fast) var(--ease-in-out),
              color var(--duration-fast) var(--ease-in-out);
  color: var(--color-text-muted);
}
.tts-button:hover:not(:disabled), .copy-button:hover:not(:disabled) {
  background: var(--color-accent-soft-bg); border-color: var(--color-accent); color: var(--color-accent);
}
.tts-button:disabled, .copy-button:disabled { cursor: not-allowed; opacity: 0.5; }
.tts-button.tts-playing { background: var(--color-accent-soft); border-color: var(--color-accent); color: var(--color-accent); }
.tts-bars {
  display: inline-flex; align-items: flex-end; gap: 2px; height: 12px;
}
.tts-bars span {
  width: 2px;
  background: var(--color-accent);
  animation: barBounce 0.6s ease-in-out infinite;
}
.tts-bars span:nth-child(1) { height: 6px; animation-delay: 0s; }
.tts-bars span:nth-child(2) { height: 11px; animation-delay: 0.15s; }
.tts-bars span:nth-child(3) { height: 8px; animation-delay: 0.3s; }
@keyframes barBounce {
  0%, 100% { transform: scaleY(0.5); }
  50% { transform: scaleY(1); }
}
.tts-spinner {
  width: 12px; height: 12px;
  border: 2px solid var(--color-border); border-top-color: var(--color-accent);
  border-radius: 50%; animation: tts-spin 0.6s linear infinite;
}
@keyframes tts-spin { to { transform: rotate(360deg); } }
.copy-button.copy-done {
  background: var(--color-success-soft);
  border-color: var(--color-success);
  color: var(--color-success);
}

.suggestion-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 10px;
  padding-top: 10px;
  border-top: 1px solid var(--color-border-light);
}
.suggestion-chip {
  padding: 4px 10px;
  border: 1px solid var(--color-border-strong);
  border-radius: var(--radius-sm);
  background: var(--color-surface);
  color: var(--color-text-secondary);
  font-size: 12px;
  font-weight: 400;
  cursor: pointer;
  transition: background var(--duration-fast) var(--ease-in-out),
              border-color var(--duration-fast) var(--ease-in-out),
              color var(--duration-fast) var(--ease-in-out);
}
.suggestion-chip:hover:not(:disabled) {
  background: var(--color-accent-soft-bg);
  border-color: var(--color-accent);
  color: var(--color-accent);
}
.suggestion-chip:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.full-width {
  width: 100%;
}

.sidebar .secondary-button.full-width {
  min-height: 32px;
  font-size: 12px;
  font-weight: 500;
}

.sidebar-empty {
  margin: auto;
  max-width: 420px;
  color: var(--color-text-muted);
  text-align: center;
  line-height: 1.7;
}

@media (max-width: 1180px) {
  .workspace {
    grid-template-columns: 1fr;
  }

  .chat-header {
    flex-direction: column;
  }

  .sidebar {
    min-height: auto;
    height: auto;
  }

  .chat-header {
    align-items: flex-start;
  }
}

@media (max-width: 720px) {
  .app-shell {
    padding: 0;
  }

  .workspace {
    gap: 0;
  }

  .chat-card,
  .sidebar {
    min-height: auto;
    height: auto;
    border-radius: 0;
    border-left: none;
    border-right: none;
  }

  .chat-card {
    min-height: 100vh;
  }

  .message {
    max-width: 100%;
  }

  .welcome-specs {
    grid-template-columns: 1fr;
    gap: 12px;
  }
  .welcome-specs > div + div {
    padding-left: 0;
    border-left: none;
    padding-top: 12px;
    border-top: 1px solid var(--color-border-light);
  }

  .composer,
  .field-row {
    flex-direction: column;
    align-items: stretch;
  }
}
</style>
