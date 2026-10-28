"""nanoka 文本的三层标记解析。

源数据里的描述文本混了三种东西。不分开处理的话,送进模型/检索的就是标记噪声:

  1. 实体引用   ``<te href=850091>今州</te>``       -> 正文 + 被引用的词条 id
  2. 富文本标签 ``<color=Highlight>`` ``<size=40>``  -> 直接剥掉
  3. 参数占位   ``{2}`` / ``{1}``                    -> **原样保留**,只登记编号

parse() 保留占位符;有同级 param 数组时可显式调用 resolve_params()。
仅展开无格式的 {n},使用零起始下标;缺值或未知格式仍保留原文。

解析器同时服务于两个下游:
  - 归一化入库(要干净正文)
  - LLM 编译(要正文 + 引用关系,用于建知识图谱的边)
所以 refs 不是副产品,它是一等输出。
"""
import re
from dataclasses import dataclass, field

# <te href=850091>今州</te> —— 内联实体引用。href 一定是纯数字。
_TE_RE = re.compile(r"<te\s+href=(\d+)\s*>(.*?)</te>", re.S)

# 富文本标签:开/闭标签都剥掉。形如 <color=Highlight> </color> <size=40> <c...>
# 用宽松匹配而不是枚举标签名 —— 源里出现过我没预期的标签(如裸 <c),
# 枚举式白名单会把它们漏在正文里。
_TAG_RE = re.compile(r"</?[A-Za-z][^<>]{0,60}>")

# {2} 或 {1:0.00%} —— 第二种形态带格式说明,同样只登记编号。
_PARAM_RE = re.compile(r"\{(\d+)(?::[^{}]*)?\}")


@dataclass
class ParsedText:
    """解析结果。text 可直接入库/送检索,refs 与 params 是结构化副产品。"""

    text: str
    refs: list[int] = field(default_factory=list)
    params: list[int] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.text.strip())


def parse(raw: str | None) -> ParsedText:
    """把一段源文本拆成 (正文, 引用, 参数占位)。"""
    if not raw:
        return ParsedText("")

    refs: list[int] = []

    def _replace_te(match: re.Match) -> str:
        # 引用标记里的文字就是可读正文,保留它;id 单独收集。
        refs.append(int(match.group(1)))
        return match.group(2)

    text = _TE_RE.sub(_replace_te, raw)
    text = _TAG_RE.sub("", text)
    params = sorted({int(m.group(1)) for m in _PARAM_RE.finditer(text)})

    # 去重但保持出现顺序 —— 顺序对"第一处引用"这类语义有用。
    seen: set[int] = set()
    ordered_refs = [r for r in refs if not (r in seen or seen.add(r))]

    return ParsedText(text=text, refs=ordered_refs, params=params)


def resolve_refs(text: str, lookup: dict[int, str], *, inline: bool = True) -> str:
    """把 ``<te href=ID>文字</te>`` 按 lookup 展开。

    inline=True  用词条名替换(用于生成可读正文)
    inline=False 直接删掉标记只留原文字(用于压缩长度)
    lookup 里没有的 id 一律保留原文字,不塞 "None" 之类的噪声。
    """

    def _replace(match: re.Match) -> str:
        hid = int(match.group(1))
        shown = match.group(2)
        if inline and hid in lookup:
            # 词条名 + 原显示文字通常是同一个词,重复时不叠加。
            name = lookup[hid]
            return name if name == shown else f"{shown}({name})"
        return shown

    return _TAG_RE.sub("", _TE_RE.sub(_replace, text or ""))


def parse_all(mapping: dict[str, str]) -> dict[str, ParsedText]:
    """对 {字段名: 原文} 批量解析,保持键。方便归一化层直接用。"""
    return {k: parse(v) for k, v in mapping.items()}


def resolve_params(text: str, values: list | None) -> str:
    """仅替换源数据明确提供的标量参数,不猜格式、不计算倍率。"""
    if not isinstance(values, list):
        return text

    def replace(match: re.Match) -> str:
        index = int(match.group(1))
        if index >= len(values) or not isinstance(values[index], (str, int, float)):
            return match.group(0)
        return str(values[index])

    return re.sub(r"\{(\d+)\}", replace, text)
