#include "sg_fft.h"

#include <math.h>

#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif

static uint16_t s_rev[SG_FFT_N];
static float    s_tw[SG_FFT_N];   /* perechi cos/sin pentru SG_FFT_N/2 rotatii */

void sg_fft_init(void)
{
  int biti = 0;
  while ((1 << biti) < SG_FFT_N)
  {
    biti++;
  }

  for (int i = 0; i < SG_FFT_N; i++)
  {
    unsigned int r = 0u;
    unsigned int x = (unsigned int)i;
    for (int b = 0; b < biti; b++)
    {
      r = (r << 1) | (x & 1u);
      x >>= 1;
    }
    s_rev[i] = (uint16_t)r;
  }

  for (int k = 0; k < SG_FFT_N / 2; k++)
  {
    const float a = -2.0f * (float)M_PI * (float)k / (float)SG_FFT_N;
    s_tw[2 * k]     = cosf(a);
    s_tw[2 * k + 1] = sinf(a);
  }
}

void sg_fft(float *b)
{
  /* permutarea cu biti inversati */
  for (int i = 0; i < SG_FFT_N; i++)
  {
    const int j = (int)s_rev[i];
    if (j > i)
    {
      const float tr = b[2 * i];
      const float ti = b[2 * i + 1];
      b[2 * i]     = b[2 * j];
      b[2 * i + 1] = b[2 * j + 1];
      b[2 * j]     = tr;
      b[2 * j + 1] = ti;
    }
  }

  /* fluturii Cooley-Tukey, decimare in timp */
  for (int lung = 2; lung <= SG_FFT_N; lung <<= 1)
  {
    const int jum = lung >> 1;
    const int pas = SG_FFT_N / lung;

    for (int i = 0; i < SG_FFT_N; i += lung)
    {
      for (int j = 0; j < jum; j++)
      {
        const int t = j * pas;
        const float wr = s_tw[2 * t];
        const float wi = s_tw[2 * t + 1];

        const int ia = 2 * (i + j);
        const int ic = 2 * (i + j + jum);

        const float xr = b[ic] * wr - b[ic + 1] * wi;
        const float xi = b[ic] * wi + b[ic + 1] * wr;

        b[ic]     = b[ia]     - xr;
        b[ic + 1] = b[ia + 1] - xi;
        b[ia]     = b[ia]     + xr;
        b[ia + 1] = b[ia + 1] + xi;
      }
    }
  }
}
