"""Parse one JSON object, permitting only a narrowly provable delimiter repair."""
import json


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"JSON 键重复：{key[:60]}")
        result[key] = value
    return result


def decode_object(raw):
    edits = []
    try:
        value = json.loads(raw, object_pairs_hook=_unique)
    except json.JSONDecodeError as original:
        stack, output = [], []
        quoted = escaped = False
        for offset, ch in enumerate(raw):
            if quoted:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    quoted = False
            elif ch == '"':
                quoted = True
            elif ch in "{[":
                stack.append(ch)
            elif ch in "}]":
                opener = "{" if ch == "}" else "["
                if opener not in stack:
                    raise original
                while stack[-1] != opener:
                    if len(edits) >= 4:
                        raise original
                    closer = "}" if stack.pop() == "{" else "]"
                    output.append(closer)
                    edits.append({"offset":offset,"inserted":closer})
                stack.pop()
            output.append(ch)
        if not edits or stack or quoted:
            raise original
        value = json.loads("".join(output), object_pairs_hook=_unique)
    if not isinstance(value,dict):
        raise ValueError("模型输出须为单个 JSON 对象")
    return value, edits
