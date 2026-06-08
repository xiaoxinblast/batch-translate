# batch-translate — 批量翻译工作流

支持 mqxliff / docx / xlsx / txt 文件的批量翻译 + 校对，全自动循环。

## 快速开始

```bash
# 1. 编辑项目模板
#    data/style_guide.txt — 项目翻译规范
#    data/term_base.xlsx — 术语表（原文 | 译文 | 注释）

# 2. 初始化（自动检测格式）
python batch_translate/batch.py init <文件> \
  --batch-size 30 --context-size 5 \
  --terms batch_translate/data/term_base.xlsx \
  --tm batch_translate/data/tm_memory.json \
  --style-guide batch_translate/data/style_guide.txt

# 3. 获取第一批
python batch_translate/batch.py next

# 4. 翻译 → 校对 → 提交（循环）
python batch_translate/batch.py review <翻译结果.json>
python batch_translate/batch.py submit <校对结果.json>
```

## 工作流

```
源文件 → parse → 中间 JSON → 分批 → AI 翻译 → 校对 → write + TM → 循环
```

每批五步：

| 步骤 | 命令 | 输入 → 输出 |
|------|------|------------|
| 分发 | `batch.py next` | 状态 → `_batch_NNN_to_translate.json` |
| 翻译 | AI Agent | 翻译 JSON → `_batch_NNN_translated.json` |
| 校对 | `batch.py review` | 翻译结果 → `_batch_NNN_to_review.json` |
| 检查 | AI Agent | 校对 JSON → `_batch_NNN_reviewed.json` |
| 提交 | `batch.py submit` | 校对结果 → write + TM + 下一批 |

## 支持格式

| 格式 | 扩展名 | 说明 |
|------|--------|------|
| mqxliff | `.mqxliff` | MemoQ XLIFF（完整支持内联标签、状态管理） |
| docx | `.docx` | Word 文档（段落+表格，保留粗体/斜体标记） |
| xlsx | `.xlsx/.xlsm` | Excel（指定源/目标列，保留同行上下文） |
| txt | `.txt/.csv/.tsv` | 纯文本（按行解析，自动检测编码） |

## 文件说明

| 文件 | 用途 |
|------|------|
| `batch.py` | 工作流编排（init/next/review/submit/status） |
| `convert.py` | 格式转换层（parse → 中间 JSON，write → 原格式） |
| `mqxliff_tool.py` | mqxliff 解析/导出/写回（含内联标签、TM） |
| `term_base.py` | 术语库 xlsx 加载 + 贪婪最长匹配 |
| `tm_store.py` | 翻译记忆 JSON 存储 + difflib 模糊检索 |
| `parsers/` | 各格式 parser（txt/xlsx/docx/mqxliff） |
| `data/` | 项目数据（指南、术语、记忆） |
| `exports/` | 工作文件输出 |

## 依赖

```bash
pip install lxml openpyxl python-docx
```

## Claude Code Skill

将 `batch-translate` Skill 安装到 `~/.claude/skills/` 后：

> "开始批量翻译" → 自动初始化 + 翻译 + 校对 + 循环直到完成
