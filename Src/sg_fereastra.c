#include "sg_fereastra.h"

#include <string.h>

void sg_fer_init(sg_fereastra_t *f)
{
  memset(f, 0, sizeof(*f));
}

void sg_fer_push(sg_fereastra_t *f, const float *cadru)
{
  memcpy(f->inel[f->idx], cadru, SG_N_MELS * sizeof(float));
  f->idx = (uint16_t)((f->idx + 1u) % SG_WIN_CADRE);

  if (f->umplut < SG_WIN_CADRE)
  {
    f->umplut++;
  }
  if (f->de_la_ultima < 0xFFFFu)
  {
    f->de_la_ultima++;
  }
}

bool sg_fer_gata(const sg_fereastra_t *f)
{
  return (f->umplut >= SG_WIN_CADRE) && (f->de_la_ultima >= SG_WIN_PAS);
}

void sg_fer_extrage(sg_fereastra_t *f, float *dest)
{
  /* Cel mai vechi cadru e chiar urmatorul care ar fi suprascris. */
  const uint16_t start = f->idx;

  for (uint16_t t = 0u; t < SG_WIN_CADRE; t++)
  {
    const float *c = f->inel[(start + t) % SG_WIN_CADRE];
    for (int b = 0; b < SG_N_MELS; b++)
    {
      dest[b * SG_WIN_CADRE + t] = c[b];
    }
  }
  f->de_la_ultima = 0u;
}
