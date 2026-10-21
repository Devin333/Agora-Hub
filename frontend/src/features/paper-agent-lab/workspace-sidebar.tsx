import { forwardRef, type Ref } from "react"
import { Bookmark, BookOpen, BrainCircuit, Layers3, Menu, Moon, Search, Sun, X } from "lucide-react"
import { TOPICS } from "./data"
import type { LibraryView, PreviewTheme, TopicId } from "./types"
import styles from "./workspace-sidebar.module.css"

export interface WorkspaceSidebarProps {
  view: LibraryView
  topic: TopicId | null
  theme: PreviewTheme
  allCount: number
  unreadCount: number
  savedCount: number
  onViewChange: (view: LibraryView) => void
  onTopicChange: (topic: TopicId) => void
  onThemeChange: (theme: PreviewTheme) => void
  onClose: () => void
  closeButtonRef?: Ref<HTMLButtonElement>
}

const VIEW_ITEMS = [
  { id: "all", label: "全部论文", icon: BookOpen },
  { id: "unread", label: "未读", icon: Menu },
  { id: "saved", label: "我的收藏", icon: Bookmark },
] as const

const TOPIC_ICONS = {
  memory: BrainCircuit,
  retrieval: Search,
  foundation: Layers3,
} as const

export const WorkspaceSidebar = forwardRef<HTMLElement, WorkspaceSidebarProps>(function WorkspaceSidebar(
  {
    view,
    topic,
    theme,
    allCount,
    unreadCount,
    savedCount,
    onViewChange,
    onTopicChange,
    onThemeChange,
    onClose,
    closeButtonRef,
  },
  ref,
) {
  const counts: Record<LibraryView, number> = {
    all: allCount,
    unread: unreadCount,
    saved: savedCount,
  }

  return (
    <aside aria-label="论文工作台导航" className={styles.sidebar} ref={ref}>
      <div className={styles.brandRow}>
        <div className={styles.brand}>
          <strong>Agora</strong>
        </div>
        <button
          aria-label="关闭导航菜单"
          className={styles.closeButton}
          onClick={onClose}
          ref={closeButtonRef}
          type="button"
        >
          <X aria-hidden="true" size={17} />
        </button>
      </div>

      <nav className={styles.navigation} aria-label="论文视图">
        <p className={styles.sectionLabel}>资料库</p>
        <div className={styles.navList}>
          {VIEW_ITEMS.map((item) => {
            const Icon = item.icon
            const selected = topic === null && view === item.id
            return (
              <button
                aria-current={selected ? "page" : undefined}
                className={styles.navItem}
                data-active={selected || undefined}
                key={item.id}
                onClick={() => onViewChange(item.id)}
                type="button"
              >
                <Icon aria-hidden="true" size={15} strokeWidth={1.8} />
                <span>{item.label}</span>
                <span className={styles.count}>{counts[item.id]}</span>
              </button>
            )
          })}
        </div>

        <p className={styles.sectionLabel}>研究主题</p>
        <div className={styles.navList}>
          {TOPICS.map((item) => {
            const Icon = TOPIC_ICONS[item.id]
            const selected = topic === item.id
            return (
              <button
                aria-current={selected ? "page" : undefined}
                className={styles.navItem}
                data-active={selected || undefined}
                key={item.id}
                onClick={() => onTopicChange(item.id)}
                type="button"
              >
                <Icon aria-hidden="true" size={15} strokeWidth={1.8} />
                <span>{item.label}</span>
              </button>
            )
          })}
        </div>
      </nav>

      <div className={styles.sidebarFooter}>
        <div className={styles.previewBoundary}>
          <span className={styles.previewDot} aria-hidden="true" />
          <span><strong>界面预览</strong><small>页面内存 · 不连接服务</small></span>
        </div>
        <div className={styles.themeSwitch} aria-label="界面主题">
          <button
            aria-label="使用白色主题"
            aria-pressed={theme === "light"}
            data-active={theme === "light" || undefined}
            onClick={() => onThemeChange("light")}
            type="button"
          >
            <Sun aria-hidden="true" size={14} />
            浅色
          </button>
          <button
            aria-label="使用黑色主题"
            aria-pressed={theme === "dark"}
            data-active={theme === "dark" || undefined}
            onClick={() => onThemeChange("dark")}
            type="button"
          >
            <Moon aria-hidden="true" size={14} />
            深色
          </button>
        </div>
      </div>
    </aside>
  )
})

