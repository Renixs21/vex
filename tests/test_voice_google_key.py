"""Google Chirp over REST (API key), the failure ladder (Chirp -> edge-tts -> Windows voice), limit warnings and the voice picker.
The network is mocked everywhere (httpx.MockTransport); no key or service is used."""
import base64
import io
import json
import sys
from pathlib import Path

import httpx
import numpy as np
import pytest
import soundfile as sf

from voice.tts import (Audio, GoogleTTS, TTSAuthError, TTSChain, TTSError, TTSQuotaError, TTSUnavailable, WindowsTTS)
from voice.usage import UsageStore

KEY = "AIzaSyFAKEKEY-abcdefghijklmnopqrstuvwxyz0123"


def tone(seconds=0.2, rate=24000):
    return (0.3 * np.sin(np.arange(int(seconds * rate)) / 20)).astype(np.float32)


def wav_bytes(seconds=0.2, rate=24000):
    buf = io.BytesIO()
    sf.write(buf, tone(seconds, rate), rate, format="WAV", subtype="PCM_16")
    return buf.getvalue()


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def google(handler, **kw):
    return GoogleTTS("en-US-Chirp3-HD-Kore", "en-US", api_key=KEY, transport=httpx.MockTransport(handler), **kw)


def ok_handler(requests=None):
    def handler(request: httpx.Request):
        if requests is not None:
            requests.append(request)
        if request.url.path.endswith("/voices"):
            return httpx.Response(200, json={"voices": [{"name": "en-US-Chirp3-HD-Kore"}, {"name": "en-US-Chirp3-HD-Puck"}]})
        return httpx.Response(200, json={"audioContent": base64.b64encode(wav_bytes()).decode()})
    return handler


# ---- REST / API-key mode ---------------------------------------------------------------------------------------------------
def test_api_key_request_is_chirp3_hd_linear16_with_the_key_in_a_header_only():
    seen = []
    g = google(ok_handler(seen))
    audio = g.synthesize("Hello there")
    assert audio.rate == 24000 and audio.samples.size > 1000
    voices, synth = seen
    assert voices.method == "GET" and voices.url.params["languageCode"] == "en-US"         # the voice name is checked once
    body = json.loads(synth.content)
    assert body == {"input": {"text": "Hello there"}, "voice": {"languageCode": "en-US", "name": "en-US-Chirp3-HD-Kore"},
                    "audioConfig": {"audioEncoding": "LINEAR16", "sampleRateHertz": 24000}}
    for r in seen:
        assert r.headers["X-Goog-Api-Key"] == KEY and KEY not in str(r.url)               # never in the URL (URLs get logged)
    g.synthesize("Again")
    assert sum(1 for r in seen if r.url.path.endswith("/voices")) == 1


def test_a_wrong_voice_name_is_a_plain_error_listing_valid_voices():
    g = GoogleTTS("en-US-Chirp3-HD-Nope", "en-US", api_key=KEY, transport=httpx.MockTransport(ok_handler()))
    with pytest.raises(TTSError, match="Kore"):
        g.synthesize("hi")


def test_availability_and_mode(tmp_path):
    assert not GoogleTTS().available()
    assert GoogleTTS(api_key=KEY).available() and GoogleTTS(api_key=KEY).mode == "api-key"
    cred = tmp_path / "key.json"
    cred.write_text("{}")
    assert GoogleTTS(credentials=cred).available() and GoogleTTS(credentials=cred).mode == "service-account"
    assert GoogleTTS(credentials=cred, api_key=KEY).mode == "api-key"                       # the API key wins
    assert not GoogleTTS(credentials=tmp_path / "missing.json").available()


@pytest.mark.parametrize("status,body,expected", [
    (429, {"error": {"message": "Quota exceeded for quota metric", "status": "RESOURCE_EXHAUSTED"}}, TTSQuotaError),
    (403, {"error": {"message": "This API method requires billing to be enabled.", "status": "PERMISSION_DENIED"}}, TTSAuthError),
    (400, {"error": {"message": "API key not valid. Please pass a valid API key.", "status": "INVALID_ARGUMENT"}}, TTSAuthError),
    (401, {"error": {"message": "Request had invalid authentication credentials."}}, TTSAuthError),
])
def test_google_errors_become_quota_or_auth_errors(status, body, expected):
    g = google(lambda r: httpx.Response(status, json=body), validate_voice=False)
    with pytest.raises(expected):
        g.synthesize("hi")


def test_server_errors_and_network_failures_are_plain_errors_and_never_leak_the_key():
    g = google(lambda r: httpx.Response(503, json={"error": {"message": f"backend down for {KEY}"}}), validate_voice=False)
    with pytest.raises(TTSError) as e:
        g.synthesize("hi")
    assert type(e.value) is TTSError and KEY not in str(e.value)

    def boom(request):
        raise httpx.ConnectError(f"cannot reach host (key {KEY})")

    with pytest.raises(TTSError) as e2:
        google(boom, validate_voice=False).synthesize("hi")
    assert type(e2.value) is TTSError and KEY not in str(e2.value)


# ---- the chain ------------------------------------------------------------------------------------------------------------------
class Fake:
    def __init__(self, name, error=None):
        self.name, self.error, self.calls = name, error, []

    def available(self):
        return True

    def synthesize(self, text):
        self.calls.append(text)
        if self.error:
            raise self.error
        return Audio(tone(), 24000)


def make(tmp_path, providers, **kw):
    clock = Clock()
    usage = UsageStore(tmp_path / "u.json", clock=clock)
    told = []
    return TTSChain(providers, usage, clock=clock, notify=lambda t, m: told.append((t, m)), **kw), usage, clock, told


def test_a_quota_error_pauses_google_for_an_hour_and_tells_you_once(tmp_path):
    g, e = Fake("google", TTSQuotaError("quota")), Fake("edge")
    c, _, clock, told = make(tmp_path, [g, e])
    c.synthesize("one")
    c.synthesize("two")
    assert len(g.calls) == 1 and e.calls == ["one", "two"]
    clock.t += 3000
    c.synthesize("three")
    assert len(g.calls) == 1                                          # still paused after 50 minutes
    clock.t += 700
    g.error = None
    c.synthesize("four")
    assert g.calls[-1] == "four"                                      # an hour later it is tried again
    assert len(told) == 1 and "quota" in told[0][0].lower() and "hour" in told[0][1]


def test_a_rejected_key_switches_google_off_for_the_session_with_one_message(tmp_path):
    g, e = Fake("google", TTSAuthError("bad key")), Fake("edge")
    c, _, clock, told = make(tmp_path, [g, e])
    for _ in range(3):
        c.synthesize("x")
        clock.t += 86400
    assert len(g.calls) == 1 and len(e.calls) == 3
    assert len(told) == 1 and "rejected" in told[0][0] and "billing" in told[0][1]
    assert c.status()["paused"] == {"google": "this session"}


def test_a_network_blip_retries_after_a_minute_without_bothering_you(tmp_path):
    g, e = Fake("google", TTSError("timeout")), Fake("edge")
    c, _, clock, told = make(tmp_path, [g, e])
    c.synthesize("a")
    c.synthesize("b")
    assert len(g.calls) == 1 and not told
    clock.t += 61
    g.error = None
    c.synthesize("c")
    assert g.calls[-1] == "c"


def test_vex_is_never_silent_chirp_then_edge_then_the_windows_voice(tmp_path):
    g, e, w = Fake("google", TTSQuotaError("q")), Fake("edge", TTSError("offline")), Fake("windows")
    c, *_ = make(tmp_path, [g, e, w])
    assert c.synthesize("hello").samples.size and w.calls == ["hello"] and c.last_provider == "windows"
    w.error = TTSError("no voice installed")
    with pytest.raises(TTSUnavailable):                              # only when every rung fails does the caller show text
        make(tmp_path, [Fake("google", TTSError("x")), Fake("edge", TTSError("y")), w])[0].synthesize("hello")


def test_a_missing_key_goes_straight_to_edge(tmp_path):
    g = GoogleTTS()
    e = Fake("edge")
    c, usage, *_ = make(tmp_path, [g, e])
    c.synthesize("hello")
    assert e.calls == ["hello"] and usage.month_total("google_chars") == 0


def test_warnings_at_75_and_90_percent_once_each_and_not_again_after_a_restart(tmp_path):
    g, e = Fake("google"), Fake("edge")
    c, usage, clock, told = make(tmp_path, [g, e], monthly_limit=1000, switch_fraction=0.95, warn_fractions=(0.75, 0.9))
    c.synthesize("x" * 700)
    assert not told
    c.synthesize("x" * 60)                                            # 760 = 76%
    c.synthesize("x" * 10)
    assert len(told) == 1 and "76%" in told[0][1] and "1,000" in told[0][1]
    c.synthesize("x" * 150)                                           # 920 = 92%
    assert len(told) == 2 and "92%" in told[1][1]
    c.synthesize("x" * 5)
    assert len(told) == 2
    again = TTSChain([g, e], usage, monthly_limit=1000, switch_fraction=0.95, clock=clock, notify=lambda t, m: told.append((t, m)))
    again.synthesize("x" * 5)                                         # a restarted Vex doesn't repeat the warnings
    assert len(told) == 2


def test_status_reports_used_characters_and_pauses(tmp_path):
    g, e = Fake("google"), Fake("edge")
    c, usage, *_ = make(tmp_path, [g, e], monthly_limit=2000)
    c.synthesize("x" * 500)
    s = c.status()
    assert s["used"] == 500 and s["limit"] == 2000 and s["fraction"] == 0.25 and s["paused"] == {} and s["last_provider"] == "google"


# ---- the Windows voice ----------------------------------------------------------------------------------------------------------
def test_windows_voice_runs_powershell_with_the_text_in_the_environment_not_the_command():
    calls = []

    def run(cmd, **kw):
        calls.append((cmd, kw))
        Path(kw["env"]["VEX_WAV"]).write_bytes(wav_bytes())

    w = WindowsTTS("Microsoft Zira Desktop", run=run)
    audio = w.synthesize('Say "hi"; Remove-Item C:\\* -Recurse')
    cmd, kw = calls[0]
    assert audio.samples.size and cmd[0] == "powershell"
    assert "Remove-Item" not in " ".join(cmd) and kw["env"]["VEX_TEXT"].startswith('Say "hi"')      # text is data, never code
    assert kw["env"]["VEX_VOICE"] == "Microsoft Zira Desktop"
    assert not Path(kw["env"]["VEX_WAV"]).exists()                                                  # temp file cleaned up


def test_windows_voice_failures_are_tts_errors():
    def run(cmd, **kw):
        raise OSError("no powershell")

    with pytest.raises(TTSError):
        WindowsTTS(run=run).synthesize("hi")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
def test_the_real_windows_voice_speaks():
    audio = WindowsTTS().synthesize("Testing.")
    assert audio.samples.size > 2000 and audio.rate >= 8000


# ---- wiring ---------------------------------------------------------------------------------------------------------------------
def test_build_voice_puts_the_key_in_google_and_always_ends_with_the_windows_voice(tmp_path, monkeypatch):
    import config
    from voice import factory

    monkeypatch.setattr(config, "env", lambda name, default="": KEY if name == "GOOGLE_TTS_API_KEY" else default)
    cfg = {**config.settings["voice"], "tts_order": ["google", "edge"]}
    told = []
    stack = factory.build_voice(cfg, root=tmp_path, notify=lambda t, m: told.append((t, m)))
    names = [p.name for p in stack.chain.providers]
    assert names == ["google", "edge", "windows"]
    assert stack.chain.providers[0].mode == "api-key" and stack.chain.providers[0].available()
    assert stack.chain.notify is not None


def test_default_order_is_chirp_edge_windows():
    import config

    assert config.settings["voice"]["tts_order"] == ["google", "edge", "windows"]
    assert "Aoede" in config.settings["voice"]["google_candidates"]


# ---- the voice picker -----------------------------------------------------------------------------------------------------------
def test_voice_picker_names_and_set(monkeypatch, capsys):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools_dev"))
    import tts_voices

    assert tts_voices.full_name("Kore", "en-US") == "en-US-Chirp3-HD-Kore"
    assert tts_voices.full_name("en-GB-Chirp3-HD-Puck", "en-US") == "en-GB-Chirp3-HD-Puck"
    assert tts_voices.short_name("en-US-Chirp3-HD-Kore") == "Kore"
    import config

    saved = []
    monkeypatch.setattr(config, "set_toml_value", lambda section, key, value, path=None: saved.append((section, key, value)))
    monkeypatch.setattr(config, "env", lambda name, default="": KEY if name == "GOOGLE_TTS_API_KEY" else default)
    monkeypatch.setattr(tts_voices, "build_google", lambda cfg, root, voice: google(ok_handler()))
    assert tts_voices.main(["--set", "Puck"]) == 0
    assert saved == [("voice", "google_voice", "en-US-Chirp3-HD-Puck")]
    assert tts_voices.main(["--set", "Nonexistent"]) == 1 and len(saved) == 1                   # checked against Google's list
    assert "not a voice Google offers" in capsys.readouterr().out


def test_voice_picker_speaks_each_candidate_and_reports_usage(monkeypatch, capsys, tmp_path):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools_dev"))
    import config
    import tts_voices

    spoken = []
    monkeypatch.setattr(tts_voices, "speak", lambda audio: spoken.append(audio.samples.size))
    monkeypatch.setattr(tts_voices, "build_google", lambda cfg, root, voice: GoogleTTS(voice, "en-US", api_key=KEY, validate_voice=False,
                                                                                       transport=httpx.MockTransport(ok_handler())))
    monkeypatch.setattr(config, "ROOT", tmp_path)
    assert tts_voices.main(["--voices", "Kore", "Puck"]) == 0
    out = capsys.readouterr().out
    assert len(spoken) == 2 and "Kore" in out and "Puck" in out and "characters used" in out
    assert tts_voices.main(["--usage"]) == 0
    assert "free characters used" in capsys.readouterr().out
