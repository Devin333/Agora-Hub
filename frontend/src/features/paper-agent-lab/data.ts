import type { PreviewPaper, TopicId } from "./types"

export const TOPICS = [
  { id: "memory", label: "Agent 与记忆" },
  { id: "retrieval", label: "检索与生成" },
  { id: "foundation", label: "基础模型" },
] as const satisfies readonly { id: TopicId; label: string }[]

export const PREVIEW_PAPERS = [
  {
    id: "memgpt",
    title: "MemGPT: Towards LLMs as Operating Systems",
    titleZh: "让语言模型拥有分层记忆",
    authors: "Charles Packer et al.",
    year: 2023,
    venue: "arXiv",
    arxivId: "2310.08560",
    sourceUrl: "https://arxiv.org/abs/2310.08560",
    topic: "memory",
    tags: ["分层记忆", "Agent"],
    summary: "论文借鉴操作系统管理内存的方式，为语言模型组织不同层级的记忆，并在有限上下文内按需调入或移出材料。作者用文档分析与跨会话对话任务检验这一思路，重点是记忆管理，而非扩大底层模型窗口。",
    keyPoints: [
      "把上下文窗口视为有限的工作记忆，由系统管理外部材料。",
      "通过明确的读写与调度机制，让模型在较长任务中维护信息。",
      "评估覆盖文档分析和需要跨会话记忆的对话场景。",
    ],
    sections: [
      {
        heading: "问题意识",
        paragraphs: ["长任务产生的信息会超过单次上下文容量。论文关注的是如何管理这些信息，而不是假设窗口本身无限增长。"],
      },
      {
        heading: "阅读提示",
        paragraphs: ["可把它看作一套记忆编排方法：模型提出操作，系统负责保存、取回和维持边界。"],
      },
    ],
  },
  {
    id: "generative-agents",
    title: "Generative Agents: Interactive Simulacra of Human Behavior",
    titleZh: "从记忆到反思，再到行动",
    authors: "Joon Sung Park et al.",
    year: 2023,
    venue: "arXiv",
    arxivId: "2304.03442",
    sourceUrl: "https://arxiv.org/abs/2304.03442",
    topic: "memory",
    tags: ["记忆流", "反思"],
    summary: "论文构建了生活在模拟小镇中的生成式 Agent：它们持续记录经历，按相关性、时近性与重要性检索记忆，再形成较高层的反思与计划。研究关注这些组件如何共同支持较连贯、可观察的社会行为。",
    keyPoints: [
      "记忆流保存 Agent 经历过的事件与观察。",
      "检索会综合相关性、时间距离和重要程度。",
      "反思把零散经历组织为更高层认识，并参与后续计划。",
    ],
    sections: [
      {
        heading: "系统结构",
        paragraphs: ["观察进入记忆后，可被检索、总结为反思，并与当前情境一起影响下一步行动。"],
      },
      {
        heading: "阅读边界",
        paragraphs: ["实验展示的是模拟环境中的行为组织，不等同于对真实人类心理或社会关系的完整建模。"],
      },
    ],
  },
  {
    id: "rag",
    title: "Retrieval-Augmented Generation for Knowledge-Intensive NLP Tasks",
    titleZh: "在生成之前，先找到证据",
    authors: "Patrick Lewis et al.",
    year: 2020,
    venue: "NeurIPS",
    arxivId: "2005.11401",
    sourceUrl: "https://arxiv.org/abs/2005.11401",
    topic: "retrieval",
    tags: ["RAG", "外部知识"],
    summary: "论文把参数化生成模型与可检索的外部文档记忆结合，让回答同时依赖模型参数和取回证据。作者比较了整段生成共享同一文档、以及每个 token 可使用不同文档的条件方式，并在知识密集任务上评估。",
    keyPoints: [
      "检索器从外部文档集合中提供生成时的非参数记忆。",
      "RAG-Sequence 与 RAG-Token 对检索条件的粒度不同。",
      "实验覆盖开放域问答、事实核验与知识密集生成任务。",
    ],
    sections: [
      {
        heading: "核心组合",
        paragraphs: ["系统先检索候选文档，再把文档作为生成条件；训练过程让检索与生成形成可优化的组合。"],
      },
      {
        heading: "阅读边界",
        paragraphs: ["检索增加了可引用的外部依据，但论文并未证明这种结构能够消除所有错误或幻觉。"],
      },
    ],
  },
  {
    id: "lost-middle",
    title: "Lost in the Middle: How Language Models Use Long Contexts",
    titleZh: "长上下文，不等于有效利用",
    authors: "Nelson F. Liu et al.",
    year: 2023,
    venue: "arXiv",
    arxivId: "2307.03172",
    sourceUrl: "https://arxiv.org/abs/2307.03172",
    topic: "retrieval",
    tags: ["长上下文", "评估"],
    summary: "论文研究信息放在长输入的不同位置时，模型利用它的效果如何变化。作者在多文档问答与合成 key-value 检索任务中观察到明显的位置影响，提示上下文容量与稳定使用上下文并不是同一件事。",
    keyPoints: [
      "实验系统改变关键材料在输入中的位置。",
      "多文档问答和 key-value 检索提供两类互补观察。",
      "结果提醒评估者同时检查长度、位置和任务设置。",
    ],
    sections: [
      {
        heading: "评估问题",
        paragraphs: ["只报告可接受的最大输入长度，无法说明模型是否能同等利用输入开头、中部和结尾的信息。"],
      },
      {
        heading: "阅读边界",
        paragraphs: ["结论来自论文所测模型与任务，适合用作评估警示，不应外推为所有模型完全相同。"],
      },
    ],
  },
  {
    id: "react",
    title: "ReAct: Synergizing Reasoning and Acting in Language Models",
    titleZh: "让推理与行动交替发生",
    authors: "Shunyu Yao et al.",
    year: 2022,
    venue: "arXiv",
    arxivId: "2210.03629",
    sourceUrl: "https://arxiv.org/abs/2210.03629",
    topic: "memory",
    tags: ["推理", "工具行动"],
    summary: "论文提出让语言模型交替生成推理轨迹与面向外部环境的行动，并把观察结果带入下一轮判断。研究在问答和交互式决策任务中考察这种循环，强调推理、行动与反馈之间的协同。",
    keyPoints: [
      "推理轨迹帮助模型维护目标、计划与当前判断。",
      "行动从外部环境获得新观察，再进入后续推理。",
      "论文比较纯推理、纯行动与交替范式的表现。",
    ],
    sections: [
      {
        heading: "循环方式",
        paragraphs: ["每一步在思考与行动之间切换，外部观察不是终点，而是下一次判断的新材料。"],
      },
      {
        heading: "阅读边界",
        paragraphs: ["这里的行动由具体任务环境定义；论文不是一套通用电脑操作产品，也不自动授予现实系统权限。"],
      },
    ],
  },
  {
    id: "attention",
    title: "Attention Is All You Need",
    titleZh: "注意力机制构成的序列模型",
    authors: "Ashish Vaswani et al.",
    year: 2017,
    venue: "NeurIPS",
    arxivId: "1706.03762",
    sourceUrl: "https://arxiv.org/abs/1706.03762",
    topic: "foundation",
    tags: ["Transformer", "注意力"],
    summary: "论文提出 Transformer 架构，以多头注意力和前馈网络构建编码器、解码器，替代当时序列建模中常见的循环与卷积结构。原文主要通过机器翻译实验讨论训练效率与生成质量。",
    keyPoints: [
      "自注意力让序列位置直接聚合其他位置的信息。",
      "多头机制在不同表示子空间并行建立关系。",
      "位置编码为不含递归的结构补充顺序信息。",
    ],
    sections: [
      {
        heading: "架构变化",
        paragraphs: ["编码器和解码器由注意力、残差连接、归一化与前馈层组成，序列计算可以更充分地并行。"],
      },
      {
        heading: "原始证据",
        paragraphs: ["论文的主要实验场景是机器翻译；后续广泛应用属于架构演进，阅读原文时应与初始证据区分。"],
      },
    ],
  },
] as const satisfies readonly PreviewPaper[]
