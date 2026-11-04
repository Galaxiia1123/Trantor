"""Lossless storage for large model records; exported events retain ordinary JSON."""
import base64
import binascii
import hashlib
import json
import zlib


TRANSPORT_KINDS = frozenset({
    "model_input", "model_output", "report_input", "report_output",
    "analysis_input", "analysis_output", "report_batch_input", "report_batch_output",
})
HEADER_KEYS = ("purpose", "actor_id", "config_id", "input_id", "level", "batch")
MAX_PACKED_BYTES = 64 * 1024 * 1024


def _json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")


def record_bytes(event):
    data = event["data"]
    if event["kind"] in TRANSPORT_KINDS:
        original = _json(data)
        if 4096 <= len(original) <= MAX_PACKED_BYTES:
            packed = base64.b64encode(zlib.compress(original, level=3)).decode("ascii")
            if len(packed) + 512 < len(original) * 0.8:
                event = {
                    **event, "data": {key:data[key] for key in HEADER_KEYS if key in data},
                    "_packed": {"codec":"zlib-base64-v1", "bytes":len(original),
                                "sha256":hashlib.sha256(original).hexdigest(), "body":packed},
                }
    return _json(event) + b"\n"


def read_record(line):
    event = json.loads(line)
    if not isinstance(event, dict) or "_packed" not in event:
        return event
    packed = event["_packed"]
    try:
        size = packed["bytes"]
        if (event["kind"] not in TRANSPORT_KINDS or packed["codec"] != "zlib-base64-v1"
                or type(size) is not int or not 1 <= size <= MAX_PACKED_BYTES):
            raise ValueError("压缩格式不受支持")
        body = base64.b64decode(packed["body"], validate=True)
        decoder = zlib.decompressobj()
        original = decoder.decompress(body, size + 1)
        if (len(original) != size or not decoder.eof or decoder.unused_data or decoder.unconsumed_tail
                or hashlib.sha256(original).hexdigest() != packed["sha256"]):
            raise ValueError("压缩正文不完整")
        data = json.loads(original)
        if not isinstance(data, dict) or event["data"] != {key:data[key] for key in HEADER_KEYS if key in data}:
            raise ValueError("压缩正文与记录头不符")
    except (KeyError, TypeError, ValueError, binascii.Error, zlib.error):
        raise ValueError("模型记录的压缩正文损坏或格式不受支持") from None
    return {key:value for key,value in event.items() if key != "_packed"} | {"data":data}
