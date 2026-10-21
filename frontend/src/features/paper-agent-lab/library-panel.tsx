import { forwardRef, type Ref } from "react"
import { Bookmark, Check, Search } from "lucide-react"
import type { PaperSort, PreviewPaper } from "./types"
import styles from "./library-panel.module.css"

export interface LibraryPanelProps {
  papers: readonly PreviewPaper[]
  selectedPaperId: string
  savedIds: ReadonlySet<string>
  readIds: ReadonlySet<string>
  query: string
  sort: PaperSort
  heading: string
  onQueryChange: (query: string) => void
  onSortChange: (sort: PaperSort) => void
  onSelectPaper: (paperId: string) => void
  onToggleSave: (paperId: string) => void
  searchInputRef?: Ref<HTMLInputElement>
}

export const LibraryPanel = forwardRef<HTMLElement, LibraryPanelProps>(function LibraryPanel(
  {
    papers,
    selectedPaperId,
    savedIds,
    readIds,
    query,
    sort,
    heading,
    onQueryChange,
    onSortChange,
    onSelectPaper,
    onToggleSave,
    searchInputRef,
  },
  ref,
) {
  return (
    <section aria-label="论文列表" className={styles.panel} ref={ref}>
      <header className={styles.header}>
        <div className={styles.headingRow}>
          <div>
            <p className={styles.eyebrow}>Library</p>
            <h1>{heading}</h1>
          </div>
          <span className={styles.resultCount}>{papers.length} 篇</span>
        </div>
        <label className={styles.searchField}>
          <Search aria-hidden="true" size={14} strokeWidth={1.8} />
          <span className={styles.srOnly}>搜索论文</span>
          <input
            onChange={(event) => onQueryChange(event.target.value)}
            placeholder="搜索标题、作者或标签"
            ref={searchInputRef}
            type="search"
            value={query}
          />
          <kbd>Ctrl K / ⌘ K</kbd>
        </label>
        <div className={styles.listControls}>
          <span>{query ? "搜索结果" : "当前视图"}</span>
          <label>
            <span className={styles.srOnly}>论文排序</span>
            <select onChange={(event) => onSortChange(event.target.value as PaperSort)} value={sort}>
              <option value="default">默认排序</option>
              <option value="year-desc">年份从新到旧</option>
              <option value="title">标题排序</option>
            </select>
          </label>
        </div>
      </header>

      <div className={styles.paperList} role="list">
        {papers.length ? papers.map((paper) => {
          const saved = savedIds.has(paper.id)
          const read = readIds.has(paper.id)
          const selected = selectedPaperId === paper.id
          return (
            <article className={styles.paperItem} data-selected={selected || undefined} key={paper.id} role="listitem">
              <button
                aria-label={`选择论文：${paper.title}`}
                aria-pressed={selected}
                className={styles.paperSelect}
                onClick={() => onSelectPaper(paper.id)}
                type="button"
              >
                <span className={styles.paperTopline}>
                  <span className={styles.readState} data-read={read || undefined}>
                    {read ? <Check aria-hidden="true" size={10} strokeWidth={2} /> : <span aria-hidden="true" />}
                    {read ? "已读" : "未读"}
                  </span>
                  <span>{paper.year} · {paper.venue}</span>
                </span>
                <strong className={styles.titleZh}>{paper.titleZh}</strong>
                <span className={styles.titleEn}>{paper.title}</span>
                <span className={styles.authors}>{paper.authors}</span>
                <span className={styles.tags}>
                  {paper.tags.map((tag) => <span key={tag}>{tag}</span>)}
                </span>
              </button>
              <button
                aria-label={`${saved ? "取消收藏" : "收藏"}：${paper.title}`}
                aria-pressed={saved}
                className={styles.saveButton}
                data-saved={saved || undefined}
                onClick={() => onToggleSave(paper.id)}
                type="button"
              >
                <Bookmark aria-hidden="true" fill={saved ? "currentColor" : "none"} size={15} strokeWidth={1.8} />
              </button>
            </article>
          )
        }) : (
          <div className={styles.emptyState}>
            <strong>此视图暂无论文</strong>
            <p>{query ? "请尝试更短的关键词，或清除搜索条件。" : "试试切换视图，或收藏感兴趣的论文。"}</p>
          </div>
        )}
      </div>
    </section>
  )
})
