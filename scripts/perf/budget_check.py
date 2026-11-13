"""Exercise the real gallery-dl write hook with a tiny, separate byte budget."""
import importlib
import os
from pathlib import Path
import sys

root = Path("/perf-results/byte-budget-check")
root.mkdir()
source = Path(sys.argv[1])
with source.open("rb") as original:
    data = original.read(11)
sys.argv = ["gallery-dl", "--destination", str(root)]
os.environ["AUTO_GALLERY_PERF_TRACE"] = "/perf-results/byte-budget-check.jsonl"
os.environ["AUTO_GALLERY_PERF_DOWNLOAD_LIMIT"] = "10"
import sitecustomize
importlib.reload(sitecustomize)
with open(root / "first-bytes.bin", "wb") as handle:
    handle.write(data[:9])
    handle.flush()
    handle.write(data[9:11])  # must exit 74 before this write crosses 10 bytes
raise RuntimeError("Byte budget hook did not stop the over-budget write")
