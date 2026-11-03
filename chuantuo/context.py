"""Fit complete UTF-8 text evidence to a model's actual serialized input budget."""


def text_batches(items, fits):
    """Keep small batches small in memory; split a single oversized text without dropping characters."""
    batch = []
    for item in items:
        if fits([*batch, item]):
            batch.append(item)
            continue
        if batch:
            yield batch
            batch = []
        if fits([item]):
            batch.append(item)
            continue
        text = item["text"]
        offset = 0
        while offset < len(text):
            low, high = 0, len(text) - offset
            while low < high:
                size = (low + high + 1) // 2
                part = {**item, "text": text[offset:offset + size], "start": offset, "length": len(text)}
                if fits([part]):
                    low = size
                else:
                    high = size - 1
            if not low:
                raise ValueError("模型的字符限额无法容纳单条证据的编号与分段信息")
            yield [{**item, "text": text[offset:offset + low], "start": offset, "length": len(text)}]
            offset += low
    if batch:
        yield batch
