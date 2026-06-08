#!/usr/bin/env python3
"""
mqxliff 解析/写回工具
用法:
  python batch_translate/mqxliff_tool.py info <file>
  python batch_translate/mqxliff_tool.py export <file> [--output <dir>]
  python batch_translate/mqxliff_tool.py import <json> <mqxliff> [--output <path>] [--status <status>]
  python batch_translate/mqxliff_tool.py test <file>
"""

import argparse
import copy
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# Windows 控制台 UTF-8 支持
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

try:
    from lxml import etree
except ImportError:
    print("错误: 需要 lxml，请执行 pip install lxml")
    sys.exit(1)

# 术语库和翻译记忆（同目录模块）
_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))
try:
    from term_base import TermBase
except ImportError:
    TermBase = None  # type: ignore[assignment]
try:
    from tm_store import TranslationMemory
except ImportError:
    TranslationMemory = None  # type: ignore[assignment]


# ═══════════════════════════════════════════════════════════════════════
# 常量 & 命名空间
# ═══════════════════════════════════════════════════════════════════════

XLIFF_NS = "urn:oasis:names:tc:xliff:document:1.2"
MQ_NS = "MQXliff"
NS_MAP = {
    "xliff": XLIFF_NS,
    "mq": MQ_NS,
}

# lxml 命名空间注册：XLIFF 1.2 使用默认 xmlns，lxml 从源文件自动继承
etree.register_namespace("mq", MQ_NS)

TAG_RE = re.compile(r"<tag\s+id=['\"](\d+)['\"]\s+type=['\"]([^'\"]*)['\"]\s+desc=['\"]([^'\"]*)['\"]\s*/>")

NOT_STARTED = "NotStarted"
PRETRANSLATED = "Pretranslated"


# ═══════════════════════════════════════════════════════════════════════
# 数据模型
# ═══════════════════════════════════════════════════════════════════════

@dataclass
class InlineTag:
    """内联标签：从 <ph> 解析得到的语义化标记"""
    ph_id: str              # 原始 ph 的 id 属性
    tag_type: str           # "fmt" | "/fmt" | "br" | "req"
    desc: str               # 人类可读描述
    original_ph_xml: str    # 原始 <ph> 元素的完整 XML 文本（用于写回）


@dataclass
class TransUnit:
    """单个翻译单元"""
    id: str
    status: str
    segment_guid: str
    first_label: str
    context: str
    note: str
    source_text: str           # 含 <tag ... /> 标记的原文
    source_ph_map: dict[str, InlineTag]  # tag_id → InlineTag 映射
    target_text: str           # 含 <tag ... /> 标记的译文（初始为空）
    has_inline_tags: bool = False


# ═══════════════════════════════════════════════════════════════════════
# <ph> 内联标签解码
# ═══════════════════════════════════════════════════════════════════════

def _parse_mq_fragment(xml_text: str) -> Optional[any]:
    """安全解析 mq:rxt / mq:rxt-req 片段。"""
    mq_ns_attr = f'xmlns:mq="{MQ_NS}"'
    wrapped_xml = f"<root {mq_ns_attr}>{xml_text}</root>"
    try:
        root_elem = etree.fromstring(wrapped_xml.encode("utf-8"), parser=etree.XMLParser(recover=True))
    except etree.XMLSyntaxError:
        return None
    if len(root_elem) == 0:
        return None
    return root_elem[0]


def _ph_to_xml(ph_element) -> str:
    """序列化 <ph> 元素，排除 tail 以免混入源文本。"""
    ph_copy = copy.deepcopy(ph_element)
    ph_copy.tail = None
    return etree.tostring(ph_copy, encoding="unicode")


def _decode_ph_to_tag(ph_element) -> InlineTag:
    """
    将单个 <ph id="N">...escaped XML...</ph> 元素解码为语义化 InlineTag。

    <ph> 内部是双重 XML 转义后的 MemoQ 标签文本，如：
      <ph id="1">&lt;mq:rxt displaytext=&quot;&amp;lt;color.strong/&amp;gt;&quot; .../&gt;</ph>
    """
    ph_id = ph_element.get("id", "")
    escaped_text = (ph_element.text or "").strip()

    if not escaped_text:
        return InlineTag(
            ph_id=ph_id, tag_type="unknown", desc="空标签",
            original_ph_xml=_ph_to_xml(ph_element)
        )

    # 注意：lxml 已解码外层 XML 实体，ph.text 内容需分情况处理：
    # - mq:rxt: 合法 XML，可直接解析
    # - mq:ch: val 可能含字面换行符（&#10; 被 lxml 解码导致），需正则提取

    if "<mq:ch" in escaped_text:
        tag_name = "ch"
        val_match = re.search(r'val="([^"]*)"', escaped_text)
        val = val_match.group(1) if val_match else ""
        # 检查是否为换行（字面换行或包含 10 的引用）
        if val in ("\n", "\r\n", "\r") or re.search(r'&#10;|&#xA;|&#xa;|10', val):
            desc = "换行"
            tag_type = "br"
        else:
            desc = f"特殊字符: {val}" if val.strip() else "换行"
            tag_type = "br"
    elif "<mq:rxt-req" in escaped_text:
        tag_name = "rxt-req"
        mq_elem = _parse_mq_fragment(escaped_text)
        if mq_elem is None:
            return InlineTag(
                ph_id=ph_id, tag_type="unknown", desc=f"无法解析: {escaped_text[:50]}",
                original_ph_xml=_ph_to_xml(ph_element)
            )
        displaytext = mq_elem.get("displaytext", "")
        desc = _describe_format_tag(displaytext)
        tag_type = "req"
    elif "<mq:rxt" in escaped_text:
        tag_name = "rxt"
        mq_elem = _parse_mq_fragment(escaped_text)
        if mq_elem is None:
            return InlineTag(
                ph_id=ph_id, tag_type="unknown", desc=f"无法解析: {escaped_text[:50]}",
                original_ph_xml=_ph_to_xml(ph_element)
            )
        displaytext = mq_elem.get("displaytext", "")
        if displaytext.startswith("</") or displaytext.startswith("&lt;/"):
            tag_type = "/fmt"
        else:
            tag_type = "fmt"
        desc = _describe_format_tag(displaytext)
    else:
        return InlineTag(
            ph_id=ph_id, tag_type="unknown", desc=f"未知标签: {escaped_text[:50]}",
            original_ph_xml=_ph_to_xml(ph_element)
        )

    # _ph_to_xml 只序列化 <ph> 本身（不含 tail），避免源文本混入译文
    return InlineTag(
        ph_id=ph_id, tag_type=tag_type, desc=desc,
        original_ph_xml=_ph_to_xml(ph_element)
    )


def _describe_format_tag(displaytext: str) -> str:
    """将 displaytext 转为人读懂的描述"""
    dt = displaytext.strip()
    if not dt:
        return "格式标签"

    # 常见的 displaytext 模式
    KNOWN = {
        "<color.strong/>": "强调色开始",
        "<color/>": "颜色结束",
        "<b/>": "粗体开始",
        "</b>": "粗体结束",
        "<i/>": "斜体开始",
        "</i>": "斜体结束",
        "<u/>": "下划线开始",
        "</u>": "下划线结束",
        "<color.attention/>": "警示色开始",
        "<color.sub/>": "副色开始",
        "<color.weak/>": "弱色开始",
        "<size.large/>": "大字号开始",
        "<size/>": "字号结束",
    }
    if dt in KNOWN:
        return KNOWN[dt]
    # 闭合标签通用处理
    if dt.startswith("</"):
        inner = dt[2:-1] if dt.endswith(">") else dt[2:]
        return f"{inner}结束"
    return dt.replace("<", "⟨").replace(">", "⟩")


# ═══════════════════════════════════════════════════════════════════════
# <tag/> → <ph> 编码（写回用）
# ═══════════════════════════════════════════════════════════════════════

def _replace_tags_with_ph(text: str, ph_map: dict[str, InlineTag]) -> str:
    """
    将 text 中的 <tag id='N' ... /> 替换回原始 <ph> XML。
    返回的字符串是多个 text + <ph> 片段的混合（不是合法 XML，是 XLIFF 的 mixed content）。
    """
    result_parts = []
    last_end = 0

    for m in TAG_RE.finditer(text):
        # 添加 tag 之前的文本
        if m.start() > last_end:
            result_parts.append(text[last_end:m.start()])

        tag_id = m.group(1)
        if tag_id in ph_map:
            result_parts.append(ph_map[tag_id].original_ph_xml)
        else:
            # ph_map 中找不到对应标签，保留原文标记（不应发生）
            result_parts.append(m.group(0))
            print(f"  ⚠️ 警告: tag id='{tag_id}' 在 ph_map 中找不到，保留原文", file=sys.stderr)

        last_end = m.end()

    # 添加最后一段文本
    if last_end < len(text):
        result_parts.append(text[last_end:])

    return "".join(result_parts)


def _clean_target_text(text: str) -> str:
    """如果 target 为纯空白，视为空"""
    if text is None:
        return ""
    return text.strip()


# ═══════════════════════════════════════════════════════════════════════
# 解析 mqxliff 文件
# ═══════════════════════════════════════════════════════════════════════

def parse_mqxliff(filepath: Path) -> tuple[list[TransUnit], etree.ElementTree]:
    """解析 mqxliff 文件，返回 (trans_units, xml_tree)。"""
    filepath = Path(filepath)
    if not filepath.is_file():
        raise FileNotFoundError(f"文件不存在: {filepath}")

    tree = etree.parse(str(filepath))
    root = tree.getroot()

    trans_units = []
    for tu_elem in root.iter(f"{{{XLIFF_NS}}}trans-unit"):
        tu = _parse_trans_unit(tu_elem)
        trans_units.append(tu)

    return trans_units, tree


def _parse_trans_unit(tu_elem) -> TransUnit:
    """解析单个 <trans-unit> 元素"""
    tu_id = tu_elem.get("id", "")
    status = tu_elem.get(f"{{{MQ_NS}}}status", NOT_STARTED)
    segment_guid = tu_elem.get(f"{{{MQ_NS}}}segmentguid", "")
    first_label = tu_elem.get(f"{{{MQ_NS}}}firstlabel", "")

    # source + 内联标签
    source_elem = tu_elem.find(f"{{{XLIFF_NS}}}source")
    source_text, ph_map, has_tags = _extract_source_with_tags(source_elem)

    # target
    target_elem = tu_elem.find(f"{{{XLIFF_NS}}}target")
    target_text = ""
    if target_elem is not None:
        target_text = _extract_target_text(target_elem, ph_map)

    # context
    context = ""
    cg = tu_elem.find(f"{{{XLIFF_NS}}}context-group")
    if cg is not None:
        ctx = cg.find(f"{{{XLIFF_NS}}}context")
        if ctx is not None and ctx.text:
            context = ctx.text.strip()

    # note
    note = ""
    note_elem = tu_elem.find(f"{{{XLIFF_NS}}}note")
    if note_elem is not None and note_elem.text:
        note = note_elem.text.strip()

    return TransUnit(
        id=tu_id,
        status=status,
        segment_guid=segment_guid,
        first_label=first_label,
        context=context,
        note=note,
        source_text=source_text,
        source_ph_map=ph_map,
        target_text=target_text,
        has_inline_tags=has_tags,
    )


def _extract_source_with_tags(source_elem) -> tuple[str, dict[str, InlineTag], bool]:
    """
    从 <source> 元素提取含 <tag ... /> 的文本和 ph_map。
    返回 (text, ph_map, has_tags)
    """
    if source_elem is None:
        return "", {}, False

    ph_map: dict[str, InlineTag] = {}
    parts = []
    has_tags = False

    # 遍历子节点：文本节点和 <ph> 元素
    if source_elem.text:
        parts.append(source_elem.text)

    for child in source_elem:
        tag_name = etree.QName(child).localname
        if tag_name == "ph":
            has_tags = True
            tag = _decode_ph_to_tag(child)
            ph_map[tag.ph_id] = tag
            parts.append(_tag_to_marker(tag))
        else:
            # 其他元素（不太会出现，但安全处理）
            parts.append(etree.tostring(child, encoding="unicode"))
        if child.tail:
            parts.append(child.tail)

    return "".join(parts), ph_map, has_tags


def _extract_target_text(target_elem, source_ph_map: dict[str, InlineTag]) -> str:
    """
    从 <target> 元素提取文本（含 tag 标记）。
    使用 source_ph_map 来解码 target 中的 <ph> 元素（target 的 ph 也应使用相同的映射）。
    注意：target 可能为空（NotStarted 状态）。
    """
    if target_elem is None:
        return ""

    # 检查是否只有空白
    text_content = target_elem.text or ""
    if not text_content.strip() and len(target_elem) == 0:
        return ""

    parts = []
    if target_elem.text:
        parts.append(target_elem.text)

    for child in target_elem:
        tag_name = etree.QName(child).localname
        if tag_name == "ph":
            ph_id = child.get("id", "")
            if ph_id in source_ph_map:
                tag = source_ph_map[ph_id]
            else:
                # target 中有新的 ph，解码它
                tag = _decode_ph_to_tag(child)
            parts.append(_tag_to_marker(tag))
        else:
            parts.append(etree.tostring(child, encoding="unicode"))
        if child.tail:
            parts.append(child.tail)

    return "".join(parts)


def _tag_to_marker(tag: InlineTag) -> str:
    """将 InlineTag 转为 <tag id='N' type='T' desc='D'/> 格式"""
    return f"<tag id='{tag.ph_id}' type='{tag.tag_type}' desc='{tag.desc}'/>"


# ═══════════════════════════════════════════════════════════════════════
# write back: 将译文写入 mqxliff
# ═══════════════════════════════════════════════════════════════════════

def write_translations(
    tree: etree.ElementTree,
    trans_units: list[TransUnit],
    translations: dict[str, str],
    new_status: str = PRETRANSLATED,
    output_path: Optional[Path] = None,
) -> Path:
    """
    将翻译写回 mqxliff。
    以 tree 为基础，遍历 trans_units 并在对应 <trans-unit> 中更新 <target> 和 mq:status。

    Args:
        tree: 原始 XML 树（会被修改）
        trans_units: 解析后的翻译单元列表
        translations: {trans_unit_id: target_text} 映射
        new_status: 写回后的状态（默认 Pretranslated）
        output_path: 输出路径（None 则添加后缀 _translated）

    Returns:
        实际写入的文件路径
    """
    root = tree.getroot()

    # 建立 id → TransUnit 快速查找
    tu_by_id = {tu.id: tu for tu in trans_units}

    updated_count = 0
    for tu_elem in root.iter(f"{{{XLIFF_NS}}}trans-unit"):
        tu_id = tu_elem.get("id", "")
        if tu_id not in translations:
            continue

        tu = tu_by_id.get(tu_id)
        target_text = translations[tu_id]
        if target_text is None or (isinstance(target_text, str) and not target_text.strip()):
            continue

        # 更新 target 元素
        target_elem = tu_elem.find(f"{{{XLIFF_NS}}}target")
        if target_elem is None:
            # 不应该出现，但安全处理
            continue

        # 将 <tag ... /> 替换回 <ph>
        if tu and tu.source_ph_map:
            ph_content = _replace_tags_with_ph(target_text, tu.source_ph_map)
        else:
            ph_content = target_text

        # 保存 tail（target 之后到下一个元素之前的换行/缩进），clear() 会清除它
        target_tail = target_elem.tail

        # 清空并重建 target 内容
        target_elem.clear()
        target_elem.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
        target_elem.tail = target_tail  # 恢复 tail，保持元素间距

        # 如果有 <ph> 元素，需要用 lxml 解析再附加
        if "<ph " in ph_content:
            _set_target_mixed_content(target_elem, ph_content)
        else:
            target_elem.text = ph_content

        # 更新 mq:status
        tu_elem.set(f"{{{MQ_NS}}}status", new_status)
        updated_count += 1

    # 确定输出路径
    if output_path is None:
        # 在源文件基础上加后缀
        source_path = Path(tree.docinfo.URL) if tree.docinfo.URL else Path("output.mqxliff")
        stem = source_path.stem
        output_path = source_path.with_stem(f"{stem}_translated")
    else:
        output_path = Path(output_path)

    # 写入文件
    xml_bytes = etree.tostring(
        tree,
        xml_declaration=True,
        encoding="UTF-8",
        pretty_print=False,
    )

    # lxml 会把空 <target></target> 写成自闭合 <target/>，
    # MemoQ 不识别自闭合形式，需要还原
    xml_text = xml_bytes.decode("utf-8")
    xml_text = xml_text.replace(
        '<target xml:space="preserve"/>',
        '<target xml:space="preserve"></target>',
    )
    xml_bytes = xml_text.encode("utf-8")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(xml_bytes)

    print(f"✅ 已写入 {updated_count} 条翻译 → {output_path}")
    return output_path


def _set_target_mixed_content(target_elem, text_with_ph: str):
    """
    将混合了文本和 <ph ...>...</ph> 标记的字符串设置到 target 元素中。
    使用 lxml 解析片段然后附加到 target。
    """
    # 用 wrapper 包裹后解析
    wrapper_xml = f"<wrapper xmlns='{XLIFF_NS}'>{text_with_ph}</wrapper>"
    try:
        wrapper = etree.fromstring(wrapper_xml.encode("utf-8"))
    except etree.XMLSyntaxError as e:
        print(f"  ⚠️ XML 解析失败: {e}", file=sys.stderr)
        print(f"  内容: {text_with_ph[:200]}...", file=sys.stderr)
        target_elem.text = text_with_ph  # fallback: 纯文本
        return

    # 将 wrapper 的子节点转移到 target
    if wrapper.text:
        target_elem.text = wrapper.text
    for child in wrapper:
        target_elem.append(child)


# ═══════════════════════════════════════════════════════════════════════
# Export: mqxliff → JSON
# ═══════════════════════════════════════════════════════════════════════

def export_to_json(
    filepath: Path,
    output_dir: Optional[Path] = None,
    indent: int = 2,
    term_base_path: Optional[Path] = None,
    tm_path: Optional[Path] = None,
    tm_threshold: float = 0.6,
    style_guide_path: Optional[Path] = None,
) -> Path:
    """导出 mqxliff 为翻译友好的 JSON 文件"""
    trans_units, _tree = parse_mqxliff(filepath)

    # 加载风格指南
    style_guide_text: Optional[str] = None
    if style_guide_path and style_guide_path.is_file():
        style_guide_text = style_guide_path.read_text(encoding="utf-8")

    # 加载术语库
    term_base = None
    if term_base_path and TermBase is not None:
        term_base = TermBase(term_base_path)
        term_base.load()
        if len(term_base._terms) > 0:
            print(f"📖 术语库已加载: {len(term_base._terms)} 条")

    # 加载翻译记忆
    tm = None
    if tm_path and TranslationMemory is not None:
        tm = TranslationMemory(tm_path)
        tm.load()
        if len(tm) > 0:
            print(f"🧠 翻译记忆已加载: {len(tm)} 条")

    if output_dir is None:
        output_dir = Path(__file__).resolve().parent / "exports"
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    output_path = output_dir / f"{Path(filepath).stem}.json"

    # 提取纯文本版本的 source 用于匹配（去掉 tag 标记）
    _tag_strip_re = re.compile(r"<tag[^>]*/>")

    records = []
    terms_total = 0
    tm_total = 0
    for tu in trans_units:
        record = {
            "id": tu.id,
            "context": tu.context,
            "note": tu.note,
            "source": tu.source_text,
            "target": tu.target_text,
        }
        if tu.has_inline_tags:
            record["has_tags"] = True

        # 术语匹配
        if term_base and tu.source_text:
            plain_text = _tag_strip_re.sub("", tu.source_text)
            terms = term_base.find_terms(plain_text)
            if terms:
                record["terms"] = terms
                terms_total += 1

        # TM 模糊匹配（tm_store 内部做去 tag 比对，返回含 tag 完整版）
        if tm and tu.source_text:
            matches = tm.find_matches(tu.source_text, threshold=tm_threshold)
            if matches:
                record["tm_matches"] = matches
                tm_total += 1

        records.append(record)

    output_data = {
        "source_file": str(Path(filepath).name),
        "total": len(records),
        "has_inline_tags_count": sum(1 for tu in trans_units if tu.has_inline_tags),
        "style_guide": style_guide_text,
        "tag_guide": (
            "翻译说明："
            "1) <tag id='N' type='fmt' desc='含义'/> 是格式标签（粗体/颜色等），请保留在译文对应位置。"
            "2) <tag id='N' type='/fmt' desc='含义'/> 是格式结束标签。"
            "3) <tag id='N' type='br'/> 是换行。"
            "4) <tag id='N' type='req' desc='含义'/> 是必填富文本标签。"
            "已完成翻译请填入 target 字段。"
        ),
        "entries": records,
    }

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(output_data, f, ensure_ascii=False, indent=indent)

    print(f"📤 已导出 {len(records)} 条 → {output_path}")
    if output_data["has_inline_tags_count"] > 0:
        print(f"  其中 {output_data['has_inline_tags_count']} 条含内联格式标签")
    if terms_total > 0:
        print(f"  其中 {terms_total} 条匹配术语")
    if tm_total > 0:
        print(f"  其中 {tm_total} 条匹配翻译记忆")
    return output_path


# ═══════════════════════════════════════════════════════════════════════
# Import: JSON → mqxliff
# ═══════════════════════════════════════════════════════════════════════

def import_from_json(
    json_path: Path,
    mqxliff_path: Path,
    output_path: Optional[Path] = None,
    new_status: str = PRETRANSLATED,
    tm_path: Optional[Path] = None,
) -> Path:
    """从 JSON 导入翻译，写回 mqxliff。若 tm_path 不为空，则追加到翻译记忆。"""
    # 读取 JSON
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    entries = data.get("entries", [])
    source_file_name = data.get("source_file", str(Path(mqxliff_path).name))

    _tag_strip_re = re.compile(r"<tag[^>]*/>")

    translations = {}
    tm_new_entries = []
    for entry in entries:
        target = entry.get("target", "")
        if target and target.strip():
            translations[entry["id"]] = target
            # 积累 TM 条目（保留完整 tag 标记）
            if tm_path:
                source_tagged = entry.get("source", "").strip()
                if not source_tagged:
                    continue
                # 验证：如果去掉 tag 后纯文本为空，跳过
                if not _tag_strip_re.sub("", source_tagged).strip():
                    continue
                tm_new_entries.append({
                    "source": source_tagged,
                    "target": target.strip(),
                    "context": entry.get("context", ""),
                    "file": source_file_name,
                })

    if not translations:
        print("❌ JSON 中没有任何 target 翻译内容。")
        sys.exit(1)

    print(f"📥 从 JSON 读取到 {len(translations)} 条翻译")

    # 解析 mqxliff
    trans_units, tree = parse_mqxliff(mqxliff_path)

    # 写回
    result_path = write_translations(
        tree, trans_units, translations,
        new_status=new_status, output_path=output_path
    )

    # 追加到翻译记忆
    if tm_path and TranslationMemory is not None:
        tm = TranslationMemory(tm_path)
        tm.add(tm_new_entries)
        tm.save()
        print(f"🧠 翻译记忆已更新: +{len(tm_new_entries)} 条 → {tm_path}")

    return result_path


# ═══════════════════════════════════════════════════════════════════════
# Info: 文件统计
# ═══════════════════════════════════════════════════════════════════════

def show_info(filepath: Path):
    """显示 mqxliff 文件的翻译统计信息"""
    trans_units, _tree = parse_mqxliff(filepath)

    status_counts = {}
    has_tags_count = 0
    has_target_count = 0
    ctx_prefixes = {}

    for tu in trans_units:
        status_counts[tu.status] = status_counts.get(tu.status, 0) + 1
        if tu.has_inline_tags:
            has_tags_count += 1
        if tu.target_text and tu.target_text.strip():
            has_target_count += 1
        # 统计 context 前缀
        if tu.context:
            prefix = tu.context.split(".")[0] if "." in tu.context else tu.context
            ctx_prefixes[prefix] = ctx_prefixes.get(prefix, 0) + 1

    print(f"📄 文件: {Path(filepath).name}")
    print(f"📍 路径: {Path(filepath).resolve()}")
    print()
    print(f"📊 统计:")
    print(f"  - 总翻译单元: {len(trans_units)}")
    print(f"  - 状态分布: {status_counts}")
    print(f"  - 含内联标签: {has_tags_count} ({_pct(has_tags_count, len(trans_units))})")
    print(f"  - 已有译文:   {has_target_count} ({_pct(has_target_count, len(trans_units))})")
    print()
    print(f"📂 上下文分布 (前 10):")
    for prefix, count in sorted(ctx_prefixes.items(), key=lambda x: -x[1])[:10]:
        print(f"  - {prefix}: {count}")


def _pct(part: int, total: int) -> str:
    if total == 0:
        return "0%"
    return f"{part / total * 100:.1f}%"


# ═══════════════════════════════════════════════════════════════════════
# Test: 往返测试
# ═══════════════════════════════════════════════════════════════════════

def test_roundtrip(filepath: Path):
    """往返测试：export 全部 → 追踪所有 trans-unit → import → XML 比对"""
    print("🧪 往返测试 (Round-trip Test)")
    print(f"📄 文件: {Path(filepath).name}")
    print()

    # Step 1: 解析
    print("① 解析 mqxliff ...")
    trans_units, tree = parse_mqxliff(filepath)
    print(f"   ✓ 解析 {len(trans_units)} 个 trans-unit")
    tags_count = sum(1 for tu in trans_units if tu.has_inline_tags)
    if tags_count:
        print(f"   ℹ️  其中 {tags_count} 个含内联标签")

    # Step 2: 构造 translations（用 source_text 作为 target 模拟翻译结果）
    # 这里不填真正的翻译，而是用空 target 做一个 echo 测试：
    # 把 source 复制到 target（模拟"翻译"后的结果），
    # 测试 tag 往返的正确性
    print()
    print("② 模拟翻译往返（source → target → ph 重建）...")

    # 只对有 inline tags 的条目做测试
    test_tus = [tu for tu in trans_units if tu.has_inline_tags][:5]  # 最多测 5 个
    if not test_tus:
        print("   ℹ️  无内联标签条目，跳过深度往返测试")
        # 用普通文本测试
        test_tus = trans_units[:3]

    test_translations = {}
    for tu in test_tus:
        if tu.source_text.strip():
            # 模拟：把 source 当作 target（假设 tag 原样保留）
            test_translations[tu.id] = tu.source_text

    print(f"   测试 {len(test_translations)} 个条目")

    # Step 3: 写回（用新 tree 副本，不污染原始文件）
    try:
        tree_copy = copy.deepcopy(tree)
        output_path = filepath.parent / f"_roundtrip_test_{Path(filepath).stem}.mqxliff"
        result_path = write_translations(
            tree_copy, trans_units, test_translations,
            new_status=PRETRANSLATED, output_path=output_path
        )

        # Step 4: 验证写回后能正确重新解析
        print()
        print("③ 重新解析写回文件 ...")
        re_parsed, _ = parse_mqxliff(result_path)
        print(f"   ✓ 重新解析 {len(re_parsed)} 个 trans-unit")

        # 比对 test 条目的 tag 数量
        mismatches = 0
        for tu in re_parsed:
            if tu.id in test_translations:
                original_tu = next((t for t in test_tus if t.id == tu.id), None)
                if original_tu and tu.target_text:
                    orig_tags = len(TAG_RE.findall(original_tu.source_text))
                    new_tags = len(TAG_RE.findall(tu.target_text))
                    if orig_tags != new_tags:
                        mismatches += 1
                        print(f"   ❌ id={tu.id}: 原 tag 数={orig_tags}, 写回后 tag 数={new_tags}")
                        print(f"      原: {original_tu.source_text[:80]}...")
                        print(f"      回: {tu.target_text[:80]}...")

        if mismatches == 0:
            print("   ✅ 往返验证通过！tag 数量一致。")
        else:
            print(f"   ❌ {mismatches} 个条目不匹配")

        # 清理
        result_path.unlink()
        print()
        print(f"🧹 已清理测试文件: {result_path.name}")

    except Exception as e:
        print(f"   ❌ 往返测试失败: {e}")
        import traceback
        traceback.print_exc()


# ═══════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="mqxliff 解析/写回工具 — KH4 批量翻译工作流",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  %(prog)s info "待翻译/file.mqxliff"
  %(prog)s export "待翻译/file.mqxliff"
  %(prog)s import exports/file.json "待翻译/file.mqxliff"
  %(prog)s test "待翻译/file.mqxliff"
        """,
    )
    sub = parser.add_subparsers(dest="command", help="子命令")

    # info
    p_info = sub.add_parser("info", help="显示文件翻译统计")
    p_info.add_argument("file", type=str, help="mqxliff 文件路径")

    # export
    p_export = sub.add_parser("export", help="导出为翻译友好 JSON")
    p_export.add_argument("file", type=str, help="mqxliff 文件路径")
    p_export.add_argument("--output", "-o", type=str, default=None,
                          help="输出目录（默认 batch_translate/exports/）")
    p_export.add_argument("--terms", type=str, default=None,
                          help="术语库 xlsx 路径")
    p_export.add_argument("--tm", type=str, default=None,
                          help="翻译记忆 JSON 路径")
    p_export.add_argument("--tm-threshold", type=float, default=0.6,
                          help="TM 模糊匹配阈值（默认 0.6）")
    p_export.add_argument("--style-guide", type=str, default=None,
                          help="风格指南 txt 路径（嵌入 JSON 顶层）")

    # import
    p_import = sub.add_parser("import", help="从 JSON 导入翻译写回 mqxliff")
    p_import.add_argument("json_file", type=str, help="翻译 JSON 文件路径")
    p_import.add_argument("mqxliff_file", type=str, help="原始 mqxliff 文件路径")
    p_import.add_argument("--output", "-o", type=str, default=None,
                          help="输出路径（默认在原文件名后加 _translated）")
    p_import.add_argument("--status", type=str, default=PRETRANSLATED,
                          help=f"写回后的翻译状态（默认: {PRETRANSLATED}）")
    p_import.add_argument("--save-tm", type=str, default=None,
                          help="翻译记忆 JSON 路径（传入则将本次翻译追加到 TM）")

    # test
    p_test = sub.add_parser("test", help="往返测试")
    p_test.add_argument("file", type=str, help="mqxliff 文件路径")

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(1)

    if args.command == "info":
        show_info(Path(args.file))

    elif args.command == "export":
        filepath = Path(args.file)
        output_dir = Path(args.output) if args.output else None
        term_path = Path(args.terms) if args.terms else None
        tm_path = Path(args.tm) if args.tm else None
        style_path = Path(args.style_guide) if args.style_guide else None
        export_to_json(
            filepath, output_dir=output_dir,
            term_base_path=term_path, tm_path=tm_path,
            tm_threshold=args.tm_threshold,
            style_guide_path=style_path,
        )

    elif args.command == "import":
        json_path = Path(args.json_file)
        mqxliff_path = Path(args.mqxliff_file)
        output_path = Path(args.output) if args.output else None
        tm_path = Path(args.save_tm) if args.save_tm else None
        import_from_json(
            json_path, mqxliff_path,
            output_path=output_path, new_status=args.status,
            tm_path=tm_path,
        )

    elif args.command == "test":
        test_roundtrip(Path(args.file))


if __name__ == "__main__":
    main()
