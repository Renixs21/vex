"""Fetch only the first N GB of the 16 GB ACAV100M feature file with parallel HTTP Range requests and save a valid, smaller .npy."""
import ast
import struct
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import numpy as np

URL = "https://huggingface.co/datasets/binhpham/livekit_wakeword_features/resolve/main/openwakeword_features_ACAV100M_2000_hrs_16bit.npy"
GB = float(sys.argv[1]) if len(sys.argv) > 1 else 2.5
OUT = r"C:\Users\sanja\wakeword_train\data\features\openwakeword_features_ACAV100M_2000_hrs_16bit.npy"


def get(start, end):
    req = urllib.request.Request(URL, headers={"Range": f"bytes={start}-{end}"})
    return urllib.request.urlopen(req, timeout=90)


r = get(0, 4095)
head = r.read()
total = int(r.headers["Content-Range"].split("/")[1])
assert head[:6] == b"\x93NUMPY", head[:6]
major = head[6]
hl = struct.unpack("<H", head[8:10])[0] if major == 1 else struct.unpack("<I", head[8:12])[0]
off = 10 if major == 1 else 12
hdr = ast.literal_eval(head[off:off + hl].decode("latin1").strip())
data_start = off + hl
dt = np.dtype(hdr["descr"])
shape = hdr["shape"]
row = int(np.prod(shape[1:])) * dt.itemsize
n_rows = min(int(GB * 1e9) // row, shape[0])
print("header", hdr, "data_start", data_start, "row bytes", row, "taking", n_rows, "rows =", n_rows * row / 1e9, "GB", flush=True)

newshape = (n_rows,) + tuple(shape[1:])
h = f"{{'descr': '{hdr['descr']}', 'fortran_order': {hdr['fortran_order']}, 'shape': {newshape}, }}"
h = (h + " " * ((64 - (10 + len(h) + 1) % 64) % 64) + "\n").encode("latin1")
hdr_bytes = b"\x93NUMPY\x01\x00" + struct.pack("<H", len(h)) + h
shift = len(hdr_bytes) - data_start
end = data_start + n_rows * row
CH = 32 * 1024 * 1024
with open(OUT, "wb") as f:
    f.write(hdr_bytes)
    f.truncate(len(hdr_bytes) + n_rows * row)
done = [0]
lock = threading.Lock()
t0 = time.time()


def work(pos):
    e = min(pos + CH, end) - 1
    for _ in range(8):
        try:
            blob = get(pos, e).read()
            assert len(blob) == e - pos + 1
            with lock:
                with open(OUT, "r+b") as f:
                    f.seek(pos + shift)
                    f.write(blob)
                done[0] += len(blob)
                print(f"{done[0] / 1e9:.2f} GB  {done[0] / 1e6 / (time.time() - t0):.1f} MB/s", flush=True)
            return
        except Exception:  # noqa: BLE001
            time.sleep(2)
    raise RuntimeError(f"chunk {pos} failed")


with ThreadPoolExecutor(12) as ex:
    list(ex.map(work, range(data_start, end, CH)))
a = np.load(OUT, mmap_mode="r")
print("saved; loads:", a.shape, a.dtype, "sample mean", float(a[:1000].astype("f4").mean()), flush=True)
