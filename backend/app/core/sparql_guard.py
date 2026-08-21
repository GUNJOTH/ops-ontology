"""只读 SPARQL 1.1 门禁的纯函数部分。

把注释、字符串字面量和 IRI 在关键字检查前掩码，避免把字面量/注释/IRI 里的
``DELETE`` 等词误判为更新操作，同时保留真实出现在这些区域之外的关键字。
"""
from __future__ import annotations


def guard_text(value: str) -> str:
    """Remove comments, quoted literals and IRIs before keyword checks.

    SPARQL permits words such as DELETE in comments, string literals and
    IRIs.  They must not be treated as update operations, while actual
    update keywords outside those regions remain blocked.
    """
    output: list[str] = []
    index = 0
    length = len(value)
    quote: str | None = None
    while index < length:
        char = value[index]
        if quote:
            if value.startswith(quote, index):
                output.extend(" " for _ in quote)
                index += len(quote)
                quote = None
                continue
            if char == "\\":
                output.append(" ")
                index += 2
                continue
            output.append(" ")
            index += 1
            continue
        if char == "#":
            while index < length and value[index] not in "\r\n":
                output.append(" ")
                index += 1
            continue
        if value.startswith('"""', index) or value.startswith("'''", index):
            quote = value[index : index + 3]
            output.extend(" " for _ in quote)
            index += 3
            continue
        if char in {'"', "'"}:
            quote = char
            output.append(" ")
            index += 1
            continue
        if char == "<":
            end = value.find(">", index + 1)
            if end >= 0:
                output.extend(" " for _ in value[index : end + 1])
                index = end + 1
                continue
        output.append(char)
        index += 1
    return "".join(output)
