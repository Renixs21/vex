"""Windows' own state of the default microphone (muted? name?): the usual reason for a mic that records pure silence."""
from __future__ import annotations

import logging

log = logging.getLogger("vex.mic")


def default_mic_muted() -> bool | None:
    """True if Windows has the default recording device muted, False if not, None if it can't be told (never raises)."""
    try:
        from comtypes import CLSCTX_ALL, CoCreateInstance
        from pycaw.api.endpointvolume import IAudioEndpointVolume
        from pycaw.api.mmdeviceapi import IMMDeviceEnumerator
        from pycaw.constants import CLSID_MMDeviceEnumerator

        enum = CoCreateInstance(CLSID_MMDeviceEnumerator, IMMDeviceEnumerator, CLSCTX_ALL)
        dev = enum.GetDefaultAudioEndpoint(1, 1)                       # eCapture, eMultimedia
        vol = dev.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None).QueryInterface(IAudioEndpointVolume)
        return bool(vol.GetMute())
    except Exception:  # noqa: BLE001
        log.debug("could not read the microphone mute state", exc_info=True)
        return None
