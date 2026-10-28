"""v2 知识库:采集、解析、归一化、检索工具。

这一层的设计前提(见 README):
  - 源数据是**版本化的静态 JSON**,不是要爬的网页
  - 原文必须本地留一份,因为源站随时可能消失
  - 数值类字段走确定性查询,不进检索、不进 LLM
"""

__all__ = ["config", "parse", "db", "nanoka", "normalize", "equipment", "audit", "build", "tools"]
