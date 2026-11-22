"""Read selected protobuf fields without materializing unrelated payloads."""


def varint(view, pos):
    value = 0
    for shift in range(0, 70, 7):
        if pos >= len(view):
            break
        byte = view[pos]
        pos += 1
        value |= (byte & 127) << shift
        if byte < 128:
            return value, pos
    raise ValueError("无效的 protobuf 整数")


def items(blob, wanted=None):
    view = memoryview(blob)
    pos = 0
    while pos < len(view):
        tag, pos = varint(view, pos)
        number, kind = tag >> 3, tag & 7
        if not number:
            raise ValueError("无效的 protobuf 字段")
        if kind == 0:
            value, pos = varint(view, pos)
        elif kind in (1, 2, 5):
            if kind == 2:
                size, pos = varint(view, pos)
            else:
                size = 8 if kind == 1 else 4
            end = pos + size
            if end > len(view):
                raise ValueError("不完整的 protobuf 记录")
            value = view[pos:end]
            pos = end
        else:
            raise ValueError("无法识别的 protobuf 类型")
        if wanted is None or number in wanted:
            yield number, value


def fields(blob, wanted=None):
    return dict(items(blob, wanted))
