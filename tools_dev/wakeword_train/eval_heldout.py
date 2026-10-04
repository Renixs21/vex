import sys, glob, wave, numpy as np
from openwakeword.model import Model
onnx = sys.argv[1]
def load(f):
    w = wave.open(f); a = np.frombuffer(w.readframes(w.getnframes()), np.int16); w.close(); return a
def score(a):
    m = Model(wakeword_models=[onnx], inference_framework="onnx")
    a = np.concatenate([np.zeros(16000, np.int16), a, np.zeros(16000, np.int16)])
    best = 0.0
    for i in range(0, len(a) - 1279, 1280):
        p = m.predict(a[i:i + 1280]); best = max(best, max(p.values()))
    return best
R = r"C:\Users\sanja\wakeword_train\real\heldout"
pos = [(f, load(f)) for f in sorted(glob.glob(R + r"\pos\*.wav"))]
pos = [(f, a) for f, a in pos if np.abs(a).max() >= 300]
negs = {}
for c in ("neg_speech", "neg_talk", "neg_typing", "neg_media", "neg_silence"):
    negs[c] = [score(load(f)) for f in sorted(glob.glob(R + rf"\{c}\*.wav"))]
ps = np.array([score(a) for f, a in pos])
allneg = np.concatenate([np.array(v) for v in negs.values()])
print(f"positives with voice: {len(ps)}  scores sorted: {np.round(np.sort(ps), 2).tolist()}")
for c, v in negs.items(): print(f"{c:11s} n={len(v):2d} max={max(v):.2f} median={np.median(v):.2f}")
print("threshold  recall  false-wakes(of %d neg clips)" % len(allneg))
for t in (0.1, 0.15, 0.2, 0.24, 0.3, 0.35, 0.4, 0.5, 0.6):
    print(f"  {t:4.2f}    {np.mean(ps >= t) * 100:5.1f}%   {int((allneg >= t).sum())}")
