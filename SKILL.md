---
name: batch-translate
description: >
  批量翻译工作流。支持 mqxliff/docx/xlsx/txt → 分批翻译 → 逐批校对 → 写回，全自动循环。
  触发条件：用户说"开始批量翻译""批量翻译这个文件""继续翻译"等。
---

# batch-translate — 批量翻译工作流

## 触发条件

用户表达要批量翻译文件时触发本 skill。如：
- "开始批量翻译"
- "批量翻译这个文件"
- "继续翻译下一批"

## 阶段〇：安装工具包

检查当前项目根目录下是否存在 `batch_translate/` 文件夹：

- **已存在** → 跳过，进入阶段一
- **不存在** → 执行：

```bash
git clone https://github.com/xiaoxinblast/batch-translate.git batch_translate
pip install lxml openpyxl python-docx pdfplumber
```

克隆后清理 `.git` 并创建必要目录：

```bash
# Windows: rmdir /s /q batch_translate\.git
# macOS/Linux: rm -rf batch_translate/.git
mkdir -p batch_translate/data batch_translate/exports
```

## 阶段一：项目初始化

若 `batch_translate/data/batch_state.json` 不存在，则尚未初始化。**自动完成以下准备**：

### 1. 生成风格指南 → `batch_translate/data/style_guide.txt`

> ⚠️ **强制步骤**：必须在 init 之前完成，不可跳过。

- 扫描项目中的翻译指南文件（如 `翻译指南/` 文件夹中的 PDF、xlsx、txt 等）
- 用 pdfplumber 提取 PDF 文本，openpyxl 读取 xlsx
- 编译为结构化的风格指南（不标注来源）
- 如项目无任何翻译文档 → 基于源文件内容分析，生成针对性的风格指南模板（术语、语气、标点约定），标注"（可按项目补充）"
- **必须用 Write 工具将风格指南写入文件**，确认文件存在后才进入步骤 2

### 2. 生成术语库 → `batch_translate/data/term_base.xlsx`

- 扫描项目中的术语数据文件（如 `_temp/术语库_*.txt`、术语表 xlsx 等）
- 以 xlsx 三列格式写入：`原文(ja) | 译文(zh) | 注释`
- 如无术语数据 → 运行 `term_base.py --create` 生成空白模板

### 3. 创建翻译记忆

自动创建空 `batch_translate/data/tm_memory.json`：

```python
import json, os
os.makedirs('batch_translate/data', exist_ok=True)
with open('batch_translate/data/tm_memory.json', 'w') as f:
    json.dump({"entries": []}, f)
```

### 4. 询问用户源文件并运行 init

```bash
python batch_translate/batch.py init <源文件> \
  --batch-chars 3000 --context-size 5 \
  --terms batch_translate/data/term_base.xlsx \
  --tm batch_translate/data/tm_memory.json \
  --style-guide batch_translate/data/style_guide.txt
```

xlsx 格式需额外指定 `--source-col A --target-col B`。

### 5. 全量语境分析

> ⚠️ **强制步骤**：init 后必须立即执行，不可跳过。

**必须**用 `Agent` 工具派分析任务。prompt：

> 读取 `batch_translate/exports/_working.json`，做全量语境分析。输出纯文本分析报告，必须覆盖：
> - **内容分类**：每个上下文分组的文本类型、用途、语气风格
> - **跨区域关联**：识别文件中相距较远但实际关联的条目（如前面是任务名/技能名，后面是详细说明），明确指出 id 范围和关联关系，提醒翻译 Agent 综合参考
> - **叙事/对话脉络**：如有故事文本或连续对话，总结剧情弧线和角色关系
> - **术语和格式模式**：高频术语、固定格式（括号/冒号/换行等）
> - **翻译注意事项**：特殊条目（空白文本、多语种混入、字数/空间约束等）

Agent 返回报告后，**必须**将内容写入 `batch_state.json` 的 `document_summary` 字段。方法：

```python
import json
state = json.load(open('batch_translate/data/batch_state.json'))
state['document_summary'] += "\n\n" + agent_report  # agent_report 是 Agent 返回的文本
json.dump(state, open('batch_translate/data/batch_state.json', 'w'), ensure_ascii=False, indent=2)
```

## 校对模式（已有译文）

如果文件已有译文，只需校对：

```bash
python batch_translate/batch.py next --review
```

直接生成校对 JSON（`_batch_NNN_to_review.json`），跳过翻译 Agent，直接走校对→submit。

## 阶段二：全自动翻译循环

反复执行以下步骤直到全部批次完成：

### Step 1: 获取当前批

```bash
python batch_translate/batch.py next
```

输出格式：`_batch_NNN_to_translate.json`

### Step 2: 翻译

用 `Agent` 工具派翻译任务。**必须指定 `model: "opus"` 并启用 max 思考强度**。prompt：

> 读取 `_batch_NNN_to_translate.json`，按其中的 instructions、style_guide 和 document_summary 翻译所有 entries 的 source 字段。
> 遇到不确定的术语或上下文时，主动搜索项目文件或联网验证。
> 翻译完成后，用 Write 工具将结果写入 `_batch_NNN_translated.json`。
> 格式：`[{"id": "1", "target": "译文"}, ...]`，仅输出 JSON。

Agent 返回后，**必须确认**文件存在，否则重试。

### Step 3: 生成校对文件

```bash
python batch_translate/batch.py review _batch_NNN_translated.json
```

输出格式：`_batch_NNN_to_review.json`（source + translated 对照）

### Step 4: 校对

用 `Agent` 工具派校对任务。**必须指定 `model: "opus"` 并启用 max 思考强度**。prompt：

> 读取 `_batch_NNN_to_review.json`，逐条核对 translated 与 source：
> 1) 术语 2) 标点 3) 语气 4) 自然流畅。
> 发现问题直接修正，不要标注。
> 修正完成后，用 Write 工具将完整的 JSON 数组写入 `_batch_NNN_reviewed.json`。
> 格式：`[{"id": "1", "target": "修正后译文"}, ...]`。
> 必须在回复中说明一共修正了几处、分别是什么问题，但修正后的完整 JSON 必须已写入文件。

Agent 返回后，**必须确认** `_batch_NNN_reviewed.json` 文件存在且包含全部条目，否则重试。

### Step 5: 提交并推进

```bash
python batch_translate/batch.py submit _batch_NNN_reviewed.json
```

submit 内部自动执行 write + TM 积累 + 重新 parse + 生成下一批 JSON。提交后回到 Step 1。

### 完成

全部批次完成后，`batch.py submit` 自动清理状态文件。告知用户完成，输出文件位置。

## 错误处理

- Agent 返回格式错误 → 重试当前 batch
- `batch.py` 命令报错 → 暂停，展示错误信息
- 用户随时可说"进度"查看 `batch.py status`

## 注意事项

- 翻译和校对 Agent 各自独立上下文
- 所有中间文件保留在 `batch_translate/exports/`
- 源文件不受影响，init 会复制到 `batch_translate/data/_working_*`
- 支持格式：mqxliff / docx / xlsx / xlsm / txt / csv / tsv
