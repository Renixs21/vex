import threading
import time

import numpy as np
import pytest

from voice.speaker import HINT, SpeechUnavailable, VoiceSpeaker, plan_speech, split_sentences
from voice.tts import Audio, TTSUnavailable


# ---- splitting and planning ---------------------------------------------------------------------------------------------------
def test_sentences_are_split_and_tiny_fragments_joined():
    assert split_sentences("Volume set to twenty percent. Anything else I can do for you today?") == [
        "Volume set to twenty percent.", "Anything else I can do for you today?"]
    assert split_sentences("Done. Opened Notepad for you and put it in front.") == ["Done. Opened Notepad for you and put it in front."]
    assert split_sentences("One line that is long enough.\nSecond line that is long enough too.") == [
        "One line that is long enough.", "Second line that is long enough too."]
    assert split_sentences("ok") == ["ok"] and split_sentences("   ") == []


def test_huge_sentences_are_split_at_commas():
    text = ("This is a very long sentence, " * 20).strip()
    chunks = split_sentences(text, max_chars=120)
    assert len(chunks) > 3 and all(len(c) <= 125 for c in chunks)
    assert " ".join(chunks).replace(" ,", ",") .count("very long sentence") == 20


def test_short_replies_are_spoken_whole():
    assert plan_speech("Two plus two equals four.", 280, 2) == ("Two plus two equals four.", "")
    exactly = "a" * 279 + "."
    assert plan_speech(exactly, 280, 2) == (exactly, "")


def test_long_replies_speak_two_sentences_and_keep_the_rest():
    text = ("The first sentence is here. The second sentence follows it. " + "The third sentence has more detail in it. " * 8).strip()
    spoken, rest = plan_speech(text, 120, 2)
    assert spoken == f"The first sentence is here. The second sentence follows it. {HINT}"
    assert rest.startswith("The third sentence") and rest.count("third sentence") == 8


def test_the_spoken_part_never_exceeds_the_limit():
    text = "Sentence number one is quite long indeed and keeps going. " + "Another long sentence appears right here now. " * 6
    spoken, rest = plan_speech(text, 70, 2)
    assert spoken.replace(HINT, "").strip() == "Sentence number one is quite long indeed and keeps going."
    assert rest.startswith("Another long")


def test_an_enormous_first_sentence_is_cut_at_a_word_and_continues_in_the_rest():
    text = "word " * 100
    spoken, rest = plan_speech(text.strip(), 60, 2)
    assert spoken.endswith(HINT) and len(spoken.replace(HINT, "")) <= 64 and rest
    assert len((spoken.replace(HINT, "") + rest).replace(".", "").split()) == 100         # nothing lost


# ---- fakes ----------------------------------------------------------------------------------------------------------------------------
class FakeChain:
    def __init__(self, delay=0.0, fail_on=None, fail_all=False):
        self.calls, self.times, self.delay, self.fail_on, self.fail_all = [], [], delay, fail_on, fail_all

    def synthesize(self, text):
        self.calls.append(text)
        self.times.append(time.monotonic())
        time.sleep(self.delay)
        if self.fail_all or (self.fail_on is not None and len(self.calls) == self.fail_on):
            raise TTSUnavailable("offline")
        return Audio(np.full(2400, len(self.calls) / 10, np.float32), 24000)


class FakePlayer:
    def __init__(self, duration=0.0):
        self.played, self.starts, self.duration, self.stops = [], [], duration, 0
        self._stop = threading.Event()

    def play(self, samples, rate, on_level):
        self.starts.append(time.monotonic())
        self.played.append(round(float(samples[0]), 1))
        on_level(0.5)
        self._stop.clear() if False else None
        self._stop.wait(self.duration)

    def stop(self):
        self.stops += 1
        self._stop.set()


def make(chain=None, player=None, **kw):
    sleeps = []
    sp = VoiceSpeaker(chain or FakeChain(), player or FakePlayer(), sleep=sleeps.append, **kw)
    sp.sleeps = sleeps
    return sp


LEVELS = []


def on_level(v):
    LEVELS.append(v)


# ---- speaking ---------------------------------------------------------------------------------------------------------------------
def test_sentences_are_spoken_in_order_with_levels_and_a_guard_pause():
    LEVELS.clear()
    sp = make()
    sp.speak("Volume set to twenty percent. Anything else I can do for you today?", on_level)
    assert sp.chain.calls == ["Volume set to twenty percent.", "Anything else I can do for you today?"]
    assert sp.player.played == [0.1, 0.2] and LEVELS[:2] == [0.5, 0.5] and LEVELS[-1] == 0.0
    assert sp.sleeps == [0.3] and not sp.has_remaining


def test_synthesis_of_the_next_sentence_overlaps_playback_of_the_current_one():
    sp = make(FakeChain(delay=0.15), FakePlayer(duration=0.4))
    sp.speak("First sentence is right here. Second sentence is right here. Third sentence is right here.", on_level)
    c, p = sp.chain.times, sp.player.starts
    assert len(c) == 3 and len(p) == 3
    assert c[1] < p[0] + 0.4 and c[2] < p[1] + 0.4          # the next sentence was requested while the previous one was still playing
    assert p[0] - c[0] < 0.4                                # speech started after one sentence's synthesis, not all three


def test_a_long_reply_speaks_a_summary_and_the_rest_can_be_read_later():
    LEVELS.clear()
    text = "First sentence here is complete. Second sentence here is complete. " + "More detail follows in this sentence now. " * 6
    sp = make(max_chars=100, max_sentences=2)
    sp.speak(text, on_level)
    assert any("The rest is on screen" in c for c in sp.chain.calls) and sp.has_remaining
    sp.chain.calls.clear()
    assert sp.speak_rest(on_level) is True
    assert all("The rest is on screen" not in c for c in sp.chain.calls) and "More detail follows" in " ".join(sp.chain.calls)
    assert len(" ".join(sp.chain.calls)) > 200 and not sp.has_remaining
    assert sp.speak_rest(on_level) is False                 # nothing left now


def test_a_new_reply_replaces_the_stored_remainder():
    sp = make(max_chars=60, max_sentences=1)
    sp.speak("A long first reply sentence goes here, truly. And then there is a lot more to say after that one.", on_level)
    assert sp.has_remaining
    sp.speak("Short.", on_level)
    assert not sp.has_remaining


def test_say_speaks_exactly_the_text_and_keeps_the_remainder():
    sp = make(max_chars=60, max_sentences=1)
    sp.speak("A long first reply sentence goes here, truly. And then there is a lot more to say after that one.", on_level)
    rest = sp._remaining
    sp.chain.calls.clear()
    sp.say("Should I lock the PC? " * 1, on_level)
    assert sp.chain.calls == ["Should I lock the PC?"] and sp._remaining == rest


def test_stop_interrupts_playback_and_skips_the_rest():
    LEVELS.clear()
    player = FakePlayer(duration=5)
    sp = make(player=player)
    t = threading.Thread(target=sp.speak, args=("First sentence is right here. Second sentence is right here. Third one is here too.", on_level))
    t0 = time.monotonic()
    t.start()
    time.sleep(0.3)
    sp.stop()
    t.join(3)
    assert not t.is_alive() and time.monotonic() - t0 < 2
    assert player.stops >= 1 and len(player.played) == 1 and sp.sleeps == []          # barge-in: no guard pause, nothing more played
    assert LEVELS[-1] == 0.0


def test_stop_while_idle_is_harmless_and_speaking_works_again_afterwards():
    sp = make()
    sp.stop()
    sp.stop()
    sp.speak("Hello there, this is fine.", on_level)
    assert sp.player.played == [0.1]


def test_if_nothing_can_be_synthesised_the_first_failure_is_raised():
    sp = make(FakeChain(fail_all=True))
    with pytest.raises(SpeechUnavailable):
        sp.speak("Anything at all, said out loud.", on_level)
    assert sp.player.played == []


def test_a_failure_part_way_keeps_what_was_already_said():
    sp = make(FakeChain(fail_on=2))
    sp.speak("First sentence is right here. Second sentence is right here. Third sentence is right here.", on_level)
    assert sp.player.played == [0.1]


def test_empty_text_says_nothing():
    sp = make()
    sp.speak("   ", on_level)
    assert sp.chain.calls == [] and sp.player.played == []


def test_the_speaker_fits_the_ui_speaker_protocol():
    from ui.speaker import Speaker

    sp = make()
    assert callable(sp.speak) and callable(sp.stop)
    _ = Speaker
