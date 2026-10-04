"""Run livekit-wakeword training with a weight snapshot every 4 minutes (output/<name>/snapshots) and timing in train_time.txt."""
import sys, threading, time
from pathlib import Path
import torch
from livekit.wakeword.config import load_config
from livekit.wakeword.training import trainer as T

cfg_path = sys.argv[1]
resume = sys.argv[2] if len(sys.argv) > 2 else None
config = load_config(cfg_path)
snapdir = config.model_output_dir / "snapshots"; snapdir.mkdir(parents=True, exist_ok=True)
orig_init = T.WakeWordTrainer.__init__
def patched(self, *a, **k):
    orig_init(self, *a, **k)
    if resume:
        self.model.load_state_dict(torch.load(resume, map_location="cpu")); print("resumed weights from", resume, flush=True)
    def snap():
        i = 0
        while True:
            time.sleep(240); i += 1
            torch.save(self.model.state_dict(), snapdir / "latest.pt"); torch.save(self.model.state_dict(), snapdir / f"snap_{i:03d}.pt")
            print(f"[snapshot {i}] saved", flush=True)
    threading.Thread(target=snap, daemon=True).start()
T.WakeWordTrainer.__init__ = patched
t0 = time.time()
path = T.run_train(config)
open(config.model_output_dir / "train_time.txt", "w").write(f"{(time.time() - t0) / 60:.1f} minutes\n")
print("TRAIN DONE", path, f"{(time.time() - t0) / 60:.1f} min", flush=True)
