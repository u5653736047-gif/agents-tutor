// assistant-ui 接入(T11):流式 Markdown 的分块与残缺语法修复(纯函数)。
//
// 两个职责:
//   1. splitMarkdownBlocks——按「块级边界」把增量文本切成稳定块,供
//      AssistantMarkdown 逐块 React.memo:新 token 只重渲染末尾活跃块,
//      前面 N-1 块零开销(消除 react-markdown 每 token 全量重解析的
//      O(n²) 体感卡顿,调研文档 §3.6 的 B 方案);
//   2. repairUnclosedFence——给未闭合代码围栏的块补收尾(不修改原串,
//      只修复送入解析器的副本),避免流式中途未闭合 ``` 把后续全部内容
//      吞进代码块(调研文档 §3.6 的 A 方案)。
//
// 状态机约定(与 micromark/remark-math 的实际语法对齐):
//   - 代码围栏:行首 ``` 或 ~~~ 开闭(记录开栏字符,同字符收尾);
//     围栏内的空行绝不是分块边界(围栏必须整块原子化);
//   - 块级公式:$$ 独立成行开闭(micromark-extension-math 的 flow 语法,
//     见 assistant-markdown.test.ts 的块级公式注释);$$ 内的空行(aligned
//     多行公式常见)不是分块边界;单行 "$$E=mc^2$$" 是行内公式(同行
//     两次 $$ 计数抵消),不影响状态;
//   - 分块点:围栏与公式块之外的空行(仅由空白组成的行)。
//
// 已知取舍(与 Streamdown 等同类方案一致):跨块的链接引用定义([ref]: url)
// 与脚注在分块渲染下不再解析——教学回答几乎不用该语法,接受此边界;
// 未闭合行内 ** / ` 由解析器按字面文本容错(rehype/react-markdown 天然
// 行为),不做主动闭合(代码块内的 `x = 2 ** 3` 会被误闭合,风险大于收益)。

/** 行首围栏标记:``` 或 ~~~(至少 3 个,允许尾随语言标识) */
const FENCE_LINE = /^\s*(`{3,}|~{3,})/;

/** 把增量 Markdown 切成稳定块(块序稳定,仅末尾块随 token 增长)。 */
export function splitMarkdownBlocks(content: string): string[] {
  const lines = content.split("\n");
  const blocks: string[] = [];
  let current: string[] = [];
  // 围栏状态:开栏字符(` 或 ~);null = 不在围栏内
  let fenceChar: "`" | "~" | null = null;
  // 块级公式状态:true = 在 $$...$$ 内
  let inDisplayMath = false;

  const flush = () => {
    const block = current.join("\n").trimEnd();
    if (block.trim()) {
      blocks.push(block);
    }
    current = [];
  };

  for (const line of lines) {
    const fenceMatch = FENCE_LINE.exec(line);
    if (fenceMatch && !inDisplayMath) {
      const char = fenceMatch[1]![0] as "`" | "~";
      if (fenceChar === null) {
        fenceChar = char;
      } else if (char === fenceChar) {
        fenceChar = null;
      }
      // 不同字符的围栏行(如在 ``` 块内的 ~~~)视为普通内容
    } else if (!fenceChar) {
      // $$ 计数:奇数次切换块级公式状态(单行成对出现则抵消)
      const doubleDollarCount = (line.match(/\$\$/g) ?? []).length;
      if (doubleDollarCount % 2 === 1) {
        inDisplayMath = !inDisplayMath;
      }
    }

    const isBlank = !line.trim();
    if (isBlank && fenceChar === null && !inDisplayMath) {
      flush();
      continue;
    }
    current.push(line);
  }
  flush();
  return blocks;
}

/**
 * 修复未闭合代码围栏:块内围栏未闭合时补上收尾行(按开栏字符)。
 * 已闭合/无围栏的块原样返回(引用相等,下游 memo 不击穿)。
 * 分块器保证围栏原子化,未闭合围栏只会出现在末尾活跃块。
 */
export function repairUnclosedFence(block: string): string {
  let fenceChar: "`" | "~" | null = null;
  for (const line of block.split("\n")) {
    const match = FENCE_LINE.exec(line);
    if (!match) {
      continue;
    }
    const char = match[1]![0] as "`" | "~";
    if (fenceChar === null) {
      fenceChar = char;
    } else if (char === fenceChar) {
      fenceChar = null;
    }
  }
  if (fenceChar === null) {
    return block;
  }
  return `${block}\n${fenceChar.repeat(3)}`;
}
