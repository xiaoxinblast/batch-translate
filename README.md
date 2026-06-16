# batch-translate — 批量翻译工作流

配合 Claude Code Skill 使用的批量翻译工具，支持 日→中 翻译 + 校对 全自动循环。

## 支持格式

mqxliff（MemoQ） · docx · xlsx · txt · csv · tsv

## 快速开始

**前置：** 安装 Claude Code Skill（`batch-translate`），然后对 Claude 说：

> "开始批量翻译"

Claude 会自动完成：初始化 → 语境分析 → 翻译 → 校对 → 写回，全部自动化。

## 手动使用

```bash
# 1. 初始化（自动检测格式）
python batch_translate/batch.py init <源文件> --batch-chars 6000

# 2. 获取当前批次
python batch_translate/batch.py next

# 3. 翻译后校对
python batch_translate/batch.py review <翻译结果.json>

# 4. 提交并推进到下一批
python batch_translate/batch.py submit <校对结果.json>

# 其他
python batch_translate/batch.py status    # 查看进度
python batch_translate/batch.py next --review  # 仅校对模式（已有译文）
```

## 项目文件

| 文件 | 用途 |
|------|------|
| `data/style_guide.txt` | 翻译风格指南（共享） |
| `data/term_base.xlsx` | 术语表：原文(ja) / 译文(zh) / 注释（共享） |
| `data/tm_memory.json` | 翻译记忆（共享，自动积累） |
| `data/<项目>/` | 工作文件和状态（自动生成） |
| `exports/<项目>/` | 批次 JSON（自动生成） |

## 依赖

```bash
pip install lxml openpyxl python-docx
```
