import { computed, shallowRef, watch } from 'vue'
import { defineStore } from 'pinia'

const PAUSED_KEY = 'amis.bg-video.paused'
const SOUND_KEY = 'amis.bg-video.sound'

/** 背景音默认音量。素材内容没听过,先压到一半,免得一开场就盖过一切。 */
const BACKGROUND_VOLUME = 0.5

function readFlag(key: string): boolean | null {
  try {
    const raw = window.localStorage.getItem(key)
    if (raw === '1') return true
    if (raw === '0') return false
  } catch {
    /* 无痕模式等场景下 localStorage 可能不可用,退回默认值 */
  }
  return null
}

function writeFlag(key: string, value: boolean) {
  try {
    window.localStorage.setItem(key, value ? '1' : '0')
  } catch {
    /* 存不下来不影响本次会话 */
  }
}

function systemPrefersReducedMotion(): boolean {
  return (
    typeof window.matchMedia === 'function' &&
    window.matchMedia('(prefers-reduced-motion: reduce)').matches
  )
}

/**
 * 全页视频背景的播放状态与声音状态。
 *
 * 暂停有三个来源:用户手动开关、标签页不可见、素材不可用。分开记录是为了让
 * 「切走标签页时自动停掉解码」不和「用户手动点过暂停」互相覆盖 —— 回到页面时,
 * 只有没手动暂停过的用户才会自动续播。
 *
 * 声音必须默认关闭:浏览器只允许「静音」的视频免交互自动播放,带声音的一律要等
 * 用户先交互一次。所以音轨一直躺在文件里,只是元素初始 muted。
 */
export const useBackgroundStore = defineStore('background', () => {
  const storedPaused = readFlag(PAUSED_KEY)
  // 首次访问、用户还没表过态时,跟随系统的「减少动态效果」
  const userPaused = shallowRef(storedPaused === null ? systemPrefersReducedMotion() : storedPaused)
  const tabHidden = shallowRef(false)
  const unavailable = shallowRef(false)
  const soundOn = shallowRef(readFlag(SOUND_KEY) ?? false)
  const video = shallowRef<HTMLVideoElement | null>(null)

  const paused = computed(() => userPaused.value || tabHidden.value)

  async function syncPlayback() {
    const el = video.value
    if (!el || unavailable.value) return

    if (paused.value) {
      el.pause()
      return
    }

    el.muted = !soundOn.value
    try {
      await el.play()
    } catch {
      if (soundOn.value) {
        // 带声自动播放被浏览器拦下(本次页面加载还没有过用户交互):
        // 退回静音播放,画面不受影响,按钮同步显示成静音态。
        soundOn.value = false
        writeFlag(SOUND_KEY, false)
        el.muted = true
        try {
          await el.play()
          return
        } catch {
          /* 静音也播不了,走下面的兜底 */
        }
      }
      // 解码失败等硬错误:如实退回暂停态,按钮显示「播放」
      userPaused.value = true
      writeFlag(PAUSED_KEY, true)
    }
  }

  function attach(el: HTMLVideoElement | null) {
    video.value = el
    if (el) el.volume = BACKGROUND_VOLUME
  }

  function toggle() {
    userPaused.value = !userPaused.value
    writeFlag(PAUSED_KEY, userPaused.value)
  }

  function toggleSound() {
    soundOn.value = !soundOn.value
    writeFlag(SOUND_KEY, soundOn.value)
  }

  function onError() {
    // 素材缺失 / 解码失败 → 隐藏视频,露出 body::before 的静态底图
    unavailable.value = true
  }

  function onVisibilityChange() {
    tabHidden.value = document.hidden
  }

  function init() {
    document.addEventListener('visibilitychange', onVisibilityChange)
    tabHidden.value = document.hidden
    void syncPlayback()
  }

  function dispose() {
    document.removeEventListener('visibilitychange', onVisibilityChange)
  }

  watch([video, paused, soundOn, unavailable], () => void syncPlayback())

  return { paused, soundOn, unavailable, attach, toggle, toggleSound, onError, init, dispose }
})
