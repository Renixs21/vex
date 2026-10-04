"""Always-on "Hey Vex" wake word (openWakeWord, local, no key). Audio is scored in memory and never saved, logged or sent anywhere.

    listener = WakeWordListener(on_wake=callback, model_path="models/hey_vex.onnx", fallback_model="hey_jarvis", threshold=0.5,
                                cooldown=2.0, vad_threshold=0.5, device=None)
    listener.start()    # background thread, always listening
    listener.pause()    # while Vex records, thinks or speaks
    listener.resume()   # afterwards (resets the model's buffer)
    listener.stop()

How the pieces fit
  * The listener owns one input stream for as long as it runs. While paused it keeps the stream open but stops scoring, so a pause is
    instant and costs no re-open latency. Windows shares the microphone between streams, so the command recorder opens its own.
  * When the wake word fires the listener pauses itself *before* calling `on_wake`, so it can never fire twice or hear Vex answer.
    The controller's turn hooks resume it when the whole turn (listen, think, speak) has finished.
  * Audio that arrives after the detection (up to 1.5 s) is kept so a command said in the same breath, "Hey Vex, open notepad", is not
    lost while the chime plays and the recorder starts. `take_audio()` hands it to the recorder. Nothing from before the detection is
    kept, so the wake phrase itself never reaches speech recognition; `strip_wake_phrase` removes a transcribed tail of it.
  * If the microphone is missing or the stream dies (unplugged, default device changed) the listener tells you once and retries
    every 30 seconds. It never raises into Vex.
"""
from __future__ import annotations

import difflib
import logging
import queue
import re
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable

import numpy as np

from voice.recorder import FRAME, RATE, Recorder, to_frame

log = logging.getLogger("vex.wakeword")

CHUNK = 1280                         # openWakeWord scores 80 ms (1280 samples at 16 kHz) at a time
SETTLE_SECONDS = 0.6                 # after resume, ignore scores briefly: Vex's own voice tail and room echo
POST_WAKE_SECONDS = 1.5
LABELS = {"hey_vex": "Hey Vex", "hey_jarvis": "Hey Jarvis (fallback)"}


class _NoVad:
    """Recorder wants a VAD object; the listener only uses the recorder to open streams."""

    def is_speech(self, frame) -> bool:  # pragma: no cover - never called
        return False


def make_chime(rate: int = 24000, ms: int = 150, freq: float = 880.0, volume: float = 0.18) -> np.ndarray:
    """A short, soft sine blip with a fade in and out (no click), as float32 samples."""
    n = rate * ms // 1000
    t = np.arange(n) / rate
    env = np.sin(np.linspace(0, np.pi, n)) ** 2
    return (np.sin(2 * np.pi * freq * t) * env * volume).astype(np.float32)


def play_chime(sd_module=None, blocking: bool = True) -> None:
    """Play the wake blip on the default output. Failure is logged and ignored."""
    try:
        sd = sd_module
        if sd is None:
            import sounddevice as sd  # noqa: PLC0415
        sd.play(make_chime(), 24000)
        if blocking:
            sd.wait()
    except Exception as e:  # noqa: BLE001
        log.debug("chime failed: %s", e)


# Whisper hears "Vex" as many things: "x", "ex", "vecs", "wex", "backs", "Have x", "Havix"...
GREETINGS = {"hey", "hi", "ok", "okay", "hay", "hei", "a", "eh", "he", "hate", "have", "ay"}
WAKE_VARIANTS = {"vex", "x", "ex", "vecs", "vecks", "wex", "backs", "becks", "vax", "vix", "vexx", "vexs", "vez", "jarvis", "vegs", "veks",
                 "vics", "vicks", "wix", "weks", "vext"}
NOT_WAKE = {"vet", "ten", "yes", "web", "fix", "six", "text", "next", "vector", "flex", "vexed", "vexing", "vexes", "vest", "vets"}
# what may follow a bare "Hey Vex" without it being a command: Vex treats it as a greeting
FILLER = {"um", "uh", "erm", "er", "ah", "hmm", "mm", "okay", "ok", "so", "yeah", "yes", "yep", "please", "hello", "hi", "hey", "there",
          "are", "you", "listening", "awake", "vex", "well", "and"}
FUZZY_RATIO = 0.7
FUSED = {"havix", "heyvex", "hayvex", "hevex", "heyvecs", "havex", "hayvecs", "heyvecks", "havecs", "heyx", "heyex"}


def _tokens(text: str) -> list[tuple[str, int]]:
    """(lowercase word, end offset in text) for each word, punctuation ignored."""
    return [(m.group(0).lower(), m.end()) for m in re.finditer(r"[A-Za-z']+", text or "")]


def _is_wake_word(word: str) -> bool:
    return word in WAKE_VARIANTS or (len(word) >= 3 and word not in NOT_WAKE
                                     and difflib.SequenceMatcher(None, word, "vex").ratio() >= FUZZY_RATIO)


def wake_phrase_end(text: str) -> int:
    """Offset just after a leading wake phrase ('Hey Vex', 'Have x', 'Havix', 'Vex', 'X', 'hey ex'), or 0 if there is none."""
    words = _tokens(text)
    if not words:
        return 0
    first, end1 = words[0]
    if len(words) > 1 and first in GREETINGS and _is_wake_word(words[1][0]):          # "hey vex", "have x"
        return words[1][1]
    if _is_wake_word(first):                                                         # "vex", "x"
        return end1
    if first in FUSED:                                                               # "havix", "heyvecs"
        return end1
    return 0


def _strip_once(text: str) -> str:
    end = wake_phrase_end(text)
    if not end:
        return text.strip()
    rest = text[end:]
    if rest[:1].isalnum():                                  # "vexillology": the match was only the start of a longer word
        return text.strip()
    return rest.lstrip(" \t,.!?:;-").strip()


def strip_wake_phrase(text: str) -> str:
    """'Hey Vex, open notepad' -> 'open notepad'. Only strips at the very start (repeated: 'Hey Vex. Hey Vex, play jazz' -> 'play jazz'),
    and tolerates how Whisper mishears it ('hey vix', 'a vex', 'Have x', 'Havix')."""
    text = (text or "").strip()
    for _ in range(3):
        rest = _strip_once(text)
        if rest == text:
            break
        text = rest
    return text


def is_only_filler(text: str) -> bool:
    """'', 'um.', 'okay', 'are you there?': nothing a command could be made of."""
    words = [w for w, _ in _tokens(text)]
    return all(w in FILLER for w in words)


def is_bare_wake(raw: str) -> bool:
    """The transcript is just the wake phrase (maybe repeated, maybe with 'um' / 'okay' / 'are you there'): a greeting, not a command."""
    raw = (raw or "").strip()
    if not raw or not wake_phrase_end(raw):
        return False
    return is_only_filler(strip_wake_phrase(raw))


def label_for(model: str) -> str:
    stem = Path(model).stem.lower()
    for key, label in LABELS.items():
        if stem.startswith(key):
            return label
    return stem


def _default_model_factory(model: str, vad_threshold: float):
    from openwakeword.model import Model  # noqa: PLC0415

    return Model(wakeword_models=[model], inference_framework="onnx", vad_threshold=vad_threshold)


class WakeWordListener:
    def __init__(self, on_wake: Callable[[], object], model_path: str = "models/hey_vex.onnx", fallback_model: str = "hey_jarvis",
                 threshold: float = 0.5, cooldown: float = 2.0, vad_threshold: float = 0.5, device: str | int | None = None,
                 *, notify: Callable[[str, str], None] | None = None, stream_factory: Callable | None = None,
                 model_factory: Callable | None = None, retry_seconds: float = 30.0, clock: Callable[[], float] = time.monotonic,
                 root: Path | None = None):
        self.on_wake = on_wake
        self.model_path = model_path
        self.fallback_model = fallback_model
        self.threshold = threshold
        self.cooldown = cooldown
        self.vad_threshold = vad_threshold
        self.device = device or None
        self.notify = notify
        self.retry_seconds = retry_seconds
        self._clock = clock
        self._model_factory = model_factory or _default_model_factory
        self._recorder = Recorder(device=device, stream_factory=stream_factory, vad=_NoVad())
        root = root or Path(__file__).resolve().parent.parent
        p = Path(model_path)
        self.model_file = p if p.is_absolute() else root / p
        self.using_fallback = not self.model_file.is_file()
        self.active_model = fallback_model if self.using_fallback else str(self.model_file)
        self.label = label_for(self.active_model)
        if self.using_fallback:
            log.info("%s not found; using the built-in %s model", model_path, fallback_model)

        self._frames: "queue.Queue[np.ndarray]" = queue.Queue(maxsize=400)
        self._after: deque[np.ndarray] = deque(maxlen=int(POST_WAKE_SECONDS * RATE / FRAME))
        self._lock = threading.Lock()
        self._paused = False
        self._settle_until = 0.0
        self._last_wake = -1e9
        self._woke = False
        self._reset_pending = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._notified = False
        self.running = False                      # the model is loaded and the stream is open
        self.last_score = 0.0
        self.last_level = 0.0                  # 0..1 loudness of the latest audio frame (the Control Center's live mic meter)
        self.wakes = 0
        self.paused_peak_rms = 0.0                # loudest audio heard during the current pause (proves what the mic picked up)

    # ---- public ---------------------------------------------------------------------------------------------------------
    @property
    def paused(self) -> bool:
        return self._paused

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="wakeword", daemon=True)
        self._thread.start()
        log.info("wake word listener started (%s, threshold %.2f, cooldown %.1fs)", self.label, self.threshold, self.cooldown)

    def stop(self) -> None:
        self._stop.set()
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout=3)
        self._thread = None
        self.running = False
        log.info("wake word listener stopped")

    def pause(self) -> None:
        """Stop scoring (the stream stays open). Safe to call repeatedly."""
        with self._lock:
            if not self._paused:
                self._paused = True
                self.paused_peak_rms = 0.0
                log.info("wake word paused")

    def resume(self) -> None:
        """Start scoring again from a clean slate: the model's buffer is reset and the first moments are ignored."""
        with self._lock:
            was = self._paused
            self._paused = False
            self._woke = False
            self._reset_pending = True
            self._settle_until = self._clock() + SETTLE_SECONDS
            self._after.clear()
        self._drain()
        if was:
            log.info("wake word resumed (loudest sound heard while paused: RMS %.0f)", self.paused_peak_rms)

    def take_audio(self) -> np.ndarray | None:
        """The audio heard since the wake word (16 kHz int16), once; None if the last pause wasn't caused by a wake.

        Frames still waiting in the stream queue are included, so it is complete even when called a few milliseconds after the wake.
        """
        with self._lock:
            if not self._woke:
                return None
            frames = list(self._after)
            self._after.clear()
            try:
                while True:
                    frames.append(self._frames.get_nowait())
            except queue.Empty:
                pass
            self._woke = False
        return np.concatenate(frames) if frames else None

    def discard_audio(self) -> None:
        """Forget what was buffered since the wake (for example the chime's own sound)."""
        self._drain()                                # frames still queued came in during the chime too
        with self._lock:
            self._after.clear()

    # ---- internals ---------------------------------------------------------------------------------------------------------
    def _drain(self) -> None:
        try:
            while True:
                self._frames.get_nowait()
        except queue.Empty:
            pass

    def _fail(self, reason: str) -> None:
        self.running = False
        log.warning("wake word unavailable: %s (retrying in %.0fs)", reason, self.retry_seconds)
        if not self._notified and self.notify:
            self._notified = True
            try:
                self.notify("Wake word", "Wake word unavailable. The hotkey still works.")
            except Exception:  # noqa: BLE001
                log.debug("notify failed", exc_info=True)

    def _load_model(self):
        return self._model_factory(self.active_model, self.vad_threshold)

    def _open(self):
        rate_holder = {"rate": RATE}

        def callback(indata, n, time_info, status):  # noqa: ARG001 - PortAudio signature
            try:
                self._frames.put_nowait(to_frame(indata, rate_holder["rate"]))
            except queue.Full:
                pass
            except Exception:  # noqa: BLE001 - never raise into the audio thread
                pass

        device = self._recorder.resolve_device()
        stream, rate = self._recorder.open_stream(device, callback)
        rate_holder["rate"] = rate
        return stream

    def _run(self) -> None:
        model = stream = None
        was_down = False
        while not self._stop.is_set():
            try:
                if model is None:
                    model = self._load_model()
                if stream is None:
                    self._drain()
                    stream = self._open()
                    self.running = True
                    self._reset_pending = True
                    if was_down:
                        log.info("wake word recovered")
                    was_down = False
                    log.info("wake word listening on %s", "the default microphone" if self.device is None else self.device)
                self._loop(model, stream)
            except Exception as e:  # noqa: BLE001 - mic missing, model missing, driver errors: never take Vex down
                self._close(stream)
                stream = None
                if not was_down:
                    self._fail(str(e) or type(e).__name__)
                else:
                    log.warning("wake word still unavailable: %s", e)
                was_down = True
                self._stop.wait(self.retry_seconds)
            else:
                break
        self._close(stream)

    @staticmethod
    def _close(stream) -> None:
        if stream is None:
            return
        try:
            stream.stop()
            stream.close()
        except Exception:  # noqa: BLE001
            pass

    def _loop(self, model, stream) -> None:
        """Score audio until stopped. Raises when the stream stops delivering (so the caller reopens it)."""
        buf = np.zeros(0, dtype=np.int16)
        silent_since = self._clock()
        while not self._stop.is_set():
            try:
                frame = self._frames.get(timeout=0.4)
            except queue.Empty:
                if not getattr(stream, "active", True) or self._clock() - silent_since > 3.0:
                    raise RuntimeError("the microphone stopped delivering audio (unplugged or the default device changed)")
                continue
            silent_since = self._clock()
            rms = float(np.sqrt(np.mean(frame.astype(np.float32) ** 2)))
            self.last_level = min(1.0, (rms / 2500.0) ** 0.6)
            if self._paused:
                with self._lock:
                    self._after.append(frame)        # keep the words that follow the wake word
                self.paused_peak_rms = max(self.paused_peak_rms, float(np.sqrt(np.mean(frame.astype(np.float32) ** 2))))
                buf = np.zeros(0, dtype=np.int16)
                continue
            if self._reset_pending:
                self._reset_pending = False
                try:
                    model.reset()
                except Exception:  # noqa: BLE001
                    log.debug("model reset failed", exc_info=True)
                buf = np.zeros(0, dtype=np.int16)
            buf = np.concatenate([buf, frame])
            while len(buf) >= CHUNK:
                chunk, buf = buf[:CHUNK], buf[CHUNK:]
                self._score(model, chunk)
                if self._paused:                     # a wake just fired: what is left belongs to the command
                    for i in range(0, len(buf) - FRAME + 1, FRAME):
                        self._after.append(buf[i:i + FRAME])
                    buf = np.zeros(0, dtype=np.int16)
                    break

    def _score(self, model, chunk: np.ndarray) -> None:
        pred = model.predict(chunk)
        score = max((float(v) for v in pred.values()), default=0.0)
        self.last_score = score
        now = self._clock()
        if score < self.threshold or now < self._settle_until:
            return
        if now - self._last_wake < self.cooldown:
            log.debug("wake word score %.2f ignored (cooldown)", score)
            return
        self._last_wake = now
        self.wakes += 1
        with self._lock:
            self._paused = True
            self._woke = True
            self._after.clear()
        log.info("wake detected: %s score %.2f (threshold %.2f); paused", self.label, score, self.threshold)
        try:
            result = self.on_wake()
        except Exception:  # noqa: BLE001 - the listener must come back even if the handler blew up
            log.exception("wake handler failed")
            self.resume()
            return
        if result in ("busy", "muted"):              # no turn started, so nothing will resume us
            log.info("wake ignored: %s", result)
            self.resume()
