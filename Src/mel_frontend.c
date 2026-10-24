/* PORTAT din esp32/main/mel_frontend.c pentru STM32U5.
   Singura modificare: esp-dsp inlocuit cu sg_fft. Algoritmul, banca de
   filtre si cuantizarea raman identice, ca modelul sa vada aceleasi
   trasaturi ca la antrenare. */
#include "mel_frontend.h"

#include <math.h>
#include <string.h>

#include "sg_fft.h"

/* Daca cineva schimba n_fft in device_spec.json, vrem eroare la compilare,
   nu trasaturi gresite descoperite peste trei saptamani. */
_Static_assert(SG_N_FFT == SG_FFT_N, "SG_N_FFT difera de SG_FFT_N");

#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif

/* buffer FFT complex intercalat (re, im, re, im, ...) */
static float s_fft[SG_N_FFT * 2] __attribute__((aligned(8)));
static bool  s_fft_ready = false;

void sg_frontend_init(sg_frontend_t *fe)
{
    memset(fe, 0, sizeof(*fe));

    // biquad highpass RBJ, Q = 0.707 - aceleasi formule ca torchaudio
    const float w0 = 2.0f * (float)M_PI * SG_HP_HZ / (float)SG_SAMPLE_RATE;
    const float q  = 0.707f;
    const float alpha = sinf(w0) / (2.0f * q);
    const float cw = cosf(w0);
    const float b0 = (1.0f + cw) * 0.5f;
    const float b1 = -(1.0f + cw);
    const float b2 = b0;
    const float a0 = 1.0f + alpha;
    const float a1 = -2.0f * cw;
    const float a2 = 1.0f - alpha;
    fe->b0 = b0 / a0; fe->b1 = b1 / a0; fe->b2 = b2 / a0;
    fe->a1 = a1 / a0; fe->a2 = a2 / a0;

    if (!s_fft_ready) {
        sg_fft_init();
        s_fft_ready = true;
    }
}

static inline float biquad(sg_frontend_t *fe, float x)
{
    float y = fe->b0 * x + fe->b1 * fe->x1 + fe->b2 * fe->x2
            - fe->a1 * fe->y1 - fe->a2 * fe->y2;
    fe->x2 = fe->x1; fe->x1 = x;
    fe->y2 = fe->y1; fe->y1 = y;
    return y;
}

// DC blocker: y[n] = x[n] - x[n-1] + 0.999*y[n-1]. INMP441 are offset vizibil.
static inline float dc_block(sg_frontend_t *fe, float x)
{
    float y = x - fe->dc_x1 + 0.999f * fe->dc_y1;
    fe->dc_x1 = x;
    fe->dc_y1 = y;
    return y;
}

// Un cadru complet -> SG_N_MELS octeti.
static void frame_to_mel(const float *frame, uint8_t *out)
{
    for (int i = 0; i < SG_FRAME_LEN; i++) {
        s_fft[2 * i]     = frame[i] * sg_window[i];
        s_fft[2 * i + 1] = 0.0f;
    }
    for (int i = SG_FRAME_LEN; i < SG_N_FFT; i++) {   // zero-pad daca n_fft > frame
        s_fft[2 * i] = 0.0f;
        s_fft[2 * i + 1] = 0.0f;
    }

    sg_fft(s_fft);

    // spectru de putere, doar jumatatea utila
    static float power[SG_N_BINS];
    for (int k = 0; k < SG_N_BINS; k++) {
        const float re = s_fft[2 * k];
        const float im = s_fft[2 * k + 1];
        power[k] = re * re + im * im;
    }

    const float scale = 255.0f / (SG_LOG_MAX - SG_LOG_MIN);
    for (int m = 0; m < SG_N_MELS; m++) {
        const float *w = &sg_mel_w[sg_mel_off[m]];
        const int start = sg_mel_start[m];
        const int len   = sg_mel_len[m];
        float acc = 0.0f;
        for (int j = 0; j < len; j++) {
            acc += w[j] * power[start + j];
        }
        const float lg = log10f(acc + SG_LOG_EPS);
        float q = roundf((lg - SG_LOG_MIN) * scale);
        if (q < 0.0f)   q = 0.0f;
        if (q > 255.0f) q = 255.0f;
        out[m] = (uint8_t)q;
    }
}

int sg_frontend_process(sg_frontend_t *fe, const int16_t *pcm, int n, uint8_t *out)
{
    int frames = 0;
    int consumed = 0;

    while (consumed < n) {
        // umple bufferul pana la SG_FRAME_LEN
        int need = SG_FRAME_LEN - fe->overlap_len;
        int take = (n - consumed < need) ? (n - consumed) : need;

        for (int i = 0; i < take; i++) {
            const int16_t raw = pcm[consumed + i];
            if (raw > 32000 || raw < -32000) fe->clipped = true;
            float x = (float)raw / 32768.0f;
            x = dc_block(fe, x);
            x = biquad(fe, x);
            fe->overlap[fe->overlap_len + i] = x;
            fe->sumsq += x * x;
            fe->nsamp++;
        }
        fe->overlap_len += take;
        consumed += take;

        if (fe->overlap_len < SG_FRAME_LEN) break;   // asteptam mai mult audio

        frame_to_mel(fe->overlap, out + frames * SG_N_MELS);
        frames++;

        // gliseaza cu HOP: pastreaza coada pentru urmatorul cadru
        const int keep = SG_FRAME_LEN - SG_HOP_LEN;
        memmove(fe->overlap, fe->overlap + SG_HOP_LEN, keep * sizeof(float));
        fe->overlap_len = keep;
    }
    return frames;
}

float sg_frontend_take_rms_dbfs(sg_frontend_t *fe)
{
    if (fe->nsamp == 0) return -120.0f;
    const float rms = sqrtf(fe->sumsq / (float)fe->nsamp);
    fe->sumsq = 0.0f;
    fe->nsamp = 0;
    if (rms < 1e-7f) return -120.0f;
    return 20.0f * log10f(rms);
}

bool sg_frontend_take_clipped(sg_frontend_t *fe)
{
    const bool c = fe->clipped;
    fe->clipped = false;
    return c;
}
