"""Front-end identic cu device_spec.py v1 (ESP32/STM32), in numpy.

40 benzi mel, 200-6000 Hz, cadre de 32 ms la pas de 20 ms, log10 taiat in
[-6, 2] si cuantizat pe uint8 - exact ce trimite nodul. Modelul antrenat pe
asta consuma la rulare fix ce produce placa, fara nicio conversie.
"""
import numpy as np, wave
from scipy.signal import resample_poly

SR, FRAME, HOP, NFFT, NMEL = 16000, 512, 320, 512, 40
FMIN, FMAX, LOGMIN, LOGMAX = 200.0, 6000.0, -6.0, 2.0
WIN_FRAMES = 50          # 1.0 s


def _mel_fb():
    def hz2mel(f): return 2595 * np.log10(1 + f / 700)
    def mel2hz(m): return 700 * (10 ** (m / 2595) - 1)
    pts = mel2hz(np.linspace(hz2mel(FMIN), hz2mel(FMAX), NMEL + 2))
    bins = np.floor((NFFT + 1) * pts / SR).astype(int)
    fb = np.zeros((NMEL, NFFT // 2 + 1), np.float32)
    for m in range(NMEL):
        l, c, r = bins[m], bins[m + 1], bins[m + 2]
        if c == l: c = l + 1
        if r == c: r = c + 1
        fb[m, l:c] = (np.arange(l, c) - l) / max(c - l, 1)
        fb[m, c:r] = (r - np.arange(c, r)) / max(r - c, 1)
    return fb


FB = _mel_fb()
HANN = np.hanning(FRAME).astype(np.float32)


def citeste_wav(path):
    """WAV PCM 16/24/32 bit, orice rata -> mono float32 la 16 kHz."""
    with wave.open(str(path), "rb") as w:
        sr, n, sw, ch = w.getframerate(), w.getnframes(), w.getsampwidth(), w.getnchannels()
        raw = w.readframes(n)
    if sw == 2:
        x = np.frombuffer(raw, "<i2").astype(np.float32) / 32768
    elif sw == 4:
        x = np.frombuffer(raw, "<i4").astype(np.float32) / 2147483648
    elif sw == 3:
        a = np.frombuffer(raw, np.uint8).reshape(-1, 3).astype(np.int32)
        v = (a[:, 0] | (a[:, 1] << 8) | (a[:, 2] << 16))
        v[v >= 1 << 23] -= 1 << 24
        x = v.astype(np.float32) / 8388608
    else:
        raise ValueError(f"sampwidth {sw}")
    if ch > 1:
        x = x.reshape(-1, ch).mean(1)
    if sr != SR:
        from math import gcd
        g = gcd(int(sr), SR)
        x = resample_poly(x, SR // g, int(sr) // g).astype(np.float32)
    return x


def logmel(x):
    """(T, 40) float32 in [LOGMIN, LOGMAX] - valorile de dinaintea cuantizarii."""
    if len(x) < FRAME:
        x = np.pad(x, (0, FRAME - len(x)))
    x = x - np.mean(x)                                   # dc block
    n = 1 + (len(x) - FRAME) // HOP
    idx = np.arange(FRAME)[None, :] + HOP * np.arange(n)[:, None]
    S = np.abs(np.fft.rfft(x[idx] * HANN, NFFT, axis=1)) ** 2
    m = S @ FB.T
    return np.clip(np.log10(m + 1e-6), LOGMIN, LOGMAX).astype(np.float32)


def quant(L):
    """log-mel -> uint8, ca pe nod."""
    return np.clip((L - LOGMIN) * (255.0 / (LOGMAX - LOGMIN)), 0, 255).astype(np.uint8)


def ferestre(L, hop=25):
    """(N, 50, 40) ferestre de 1 s."""
    if len(L) < WIN_FRAMES:
        L = np.pad(L, ((0, WIN_FRAMES - len(L)), (0, 0)), constant_values=LOGMIN)
    return np.stack([L[i:i + WIN_FRAMES] for i in range(0, len(L) - WIN_FRAMES + 1, hop)])


def energie_banda(L):
    """scor pe fereastra: cat de multa energie in benzile 1-4 kHz (tipat)"""
    f = np.linspace(FMIN, FMAX, NMEL)
    b = (f > 1000) & (f < 4000)
    return (10 ** L[:, b]).sum(1) / ((10 ** L).sum(1) + 1e-9)
