# batch-translate — 批量翻译工作流

配合 Reasonix Skill 使用的批量翻译工具，支持 日→中 翻译 + 校对 全自动循环。

## 支持格式

mqxliff（MemoQ） · docx · xlsx · txt · csv · tsv

## 快速开始

**前置：** 安装 Reasonix Skill → [batch-translate-skill](https://github.com/xiaoxinblast/batch-translate-skill)，然后对话：

> "开始批量翻译"

Reasonix 会自动完成：初始化 → 语境分析 → 翻译 → 校对 → 写回，全部自动化。

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

## 翻译记忆

### 整句匹配
基于 difflib.SequenceMatcher 的整句模糊匹配，阈值 0.6。高相似度（≥0.85）可直接复用。

### 片段匹配（tm_fragments）
当整句匹配不足时自动启用。n-gram 倒排索引快速召回候选 → LCS 验证实质性重叠（≥30%）→ 最多返回 3 条不同 TM 条目。

**特性：**
- 自动排除整句已匹配的条目，无冗余
- 同片段多 TM 条目时只保留重叠度最高的一条
- 嵌套短片段自动过滤
- 全角英数归一化（ＨＰ→HP）
- 2-gram 降级索引覆盖短词

输出格式：
```json
{
  "fragment_source": "セフィロスのところへ",        // 匹配到的片段
  "match_source": "セフィロスのところへ行こう\n…",  // TM完整日文
  "match_target": "快点找到萨菲罗斯吧\n…"           // TM完整中文
}
```

## 依赖

```bash
pip install lxml openpyxl python-docx
```
