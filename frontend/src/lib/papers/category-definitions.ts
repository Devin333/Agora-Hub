import { paperMethods, paperTasks } from "./catalog"
import type { PaperCategoryDefinitions } from "./discovery-categories"
import type { PaperMethod, PaperTask } from "./types"

// Project definitions on the server; never send example metrics or paper fixtures to the directory.
export function getPaperCategoryDefinitions(methods: PaperMethod[] = [], tasks: PaperTask[] = []): PaperCategoryDefinitions {
  const labels = ({ slug, name, nameZh, description, descriptionZh }: typeof paperMethods[number]) => ({ slug, name, nameZh, description, descriptionZh })
  return {
    methods: [...methods, ...paperMethods].map(item => ({ ...labels(item), group: item.area })),
    tasks: [...tasks, ...paperTasks].map(({ slug, name, nameZh, description, descriptionZh, group }) => ({ slug, name, nameZh, description, descriptionZh, group }))
  }
}
