#include "sg_pcen.h"

#include <math.h>
#include <string.h>

/* Dezcuantizare: inversul a ce face frontendul.
   quant_scale = 255 / (log_max - log_min), deci log10(energie) = q/scale + log_min,
   iar energia liniara e 10 la puterea aia. */
#define SG_QUANT_SCALE   (255.0f / (SG_LOG_MAX - SG_LOG_MIN))

void sg_pcen_init(sg_pcen_t *p)
{
  memset(p, 0, sizeof(*p));
  p->pornit = false;
}

void sg_pcen_push(sg_pcen_t *p, const uint8_t *mel_q, float *iesire)
{
  float mel[SG_N_MELS];

  for (int i = 0; i < SG_N_MELS; i++)
  {
    const float logmel = (float)mel_q[i] / SG_QUANT_SCALE + SG_LOG_MIN;
    mel[i] = powf(10.0f, logmel);
  }

  /* La primul cadru pornim fondul din semnalul curent, ca in forward_stream.
     Daca am porni de la zero, primele secunde ar da valori absurde. */
  if (!p->pornit)
  {
    memcpy(p->m, mel, sizeof(mel));
    p->pornit = true;
  }
  else
  {
    for (int i = 0; i < SG_N_MELS; i++)
    {
      p->m[i] = (1.0f - SG_PCEN_S) * p->m[i] + SG_PCEN_S * mel[i];
    }
  }

  for (int i = 0; i < SG_N_MELS; i++)
  {
    const float alpha = sg_pcen_alpha[i];
    const float delta = sg_pcen_delta[i];
    const float r     = sg_pcen_r[i];

    /* smooth = exp(-alpha * log(m + eps)), adica (m + eps) la puterea -alpha */
    const float smooth = expf(-alpha * logf(p->m[i] + SG_PCEN_EPS));
    const float baza   = mel[i] * smooth + delta;

    iesire[i] = powf(baza, r) - powf(delta, r);
  }
}
