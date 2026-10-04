import logging, onnxruntime as ort
logging.basicConfig(level=logging.INFO)
from livekit.wakeword.config import load_config
from livekit.wakeword.data.features import run_extraction
so = ort.SessionOptions(); so.intra_op_num_threads = 10
run_extraction(load_config("configs/hey_vex.yaml"), so)
print("EXTRACT DONE")
