"""Google Takeout's classic Records.json, read as a stream."""
import json
import re


def iter_takeout_locations(stream):
    """Yield objects from a top-level `locations` array without loading the export."""
    decoder = json.JSONDecoder()
    buffer = ""
    marker = re.compile(r'"locations"\s*:\s*\[')
    while True:
        chunk = stream.read(65536)
        if not chunk:
            raise ValueError("Takeout file has no locations array")
        buffer += chunk
        match = marker.search(buffer)
        if match:
            buffer = buffer[match.end():]
            break
        if len(buffer) > 1_000_000:
            raise ValueError("Takeout locations array not found near start of file")
    while True:
        buffer = buffer.lstrip(" \t\r\n,")
        if buffer.startswith("]"):
            return
        try:
            item, end = decoder.raw_decode(buffer)
        except json.JSONDecodeError:
            chunk = stream.read(65536)
            if not chunk:
                raise ValueError("incomplete Takeout locations array")
            buffer += chunk
            continue
        if not isinstance(item, dict):
            raise ValueError("Takeout location record must be an object")
        yield item
        buffer = buffer[end:]
