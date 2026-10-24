#include "sg_gate.h"

#include <string.h>

void sg_gate_init(sg_gate_t *g)
{
  memset(g, 0, sizeof(*g));
  g->min_curent = 1e9f;
  for (int i = 0; i < SG_GATE_SUB_NR; i++)
  {
    g->inel[i] = 1e9f;
  }
}

/* Media pe benzile utile. Valorile fiind deja logaritmice, media lor e media
   geometrica a puterilor, ceea ce e mai robust decat suma: o singura banda
   foarte tare nu duce singura decizia. */
static float nivel_banda(const uint8_t *mel)
{
  int32_t s = 0;
  for (int i = SG_GATE_B_LO; i <= SG_GATE_B_HI; i++)
  {
    s += (int32_t)mel[i];
  }
  return (float)s / (float)(SG_GATE_B_HI - SG_GATE_B_LO + 1);
}

static float fond_din_inel(const sg_gate_t *g)
{
  float m = g->min_curent;
  for (int i = 0; i < SG_GATE_SUB_NR; i++)
  {
    if (g->inel[i] < m)
    {
      m = g->inel[i];
    }
  }
  return m;
}

bool sg_gate_push(sg_gate_t *g, const uint8_t *mel)
{
  g->front = false;
  g->nivel = nivel_banda(mel);

  /* Fondul se invata doar cand poarta e inchisa. Altfel evenimentul isi
     ridica singur pragul si poarta se inchide in mijlocul tipatului. */
  if (!g->activ)
  {
    if (g->nivel < g->min_curent)
    {
      g->min_curent = g->nivel;
    }
    g->sub_cadre++;
    if (g->sub_cadre >= SG_GATE_SUB_CADRE)
    {
      g->inel[g->inel_idx] = g->min_curent;
      g->inel_idx = (uint8_t)((g->inel_idx + 1u) % SG_GATE_SUB_NR);
      if (g->inel_umplut < SG_GATE_SUB_NR)
      {
        g->inel_umplut++;
      }
      g->min_curent = 1e9f;
      g->sub_cadre  = 0u;
    }
  }

  g->fond = fond_din_inel(g);

  /* Pana nu avem cel putin o secunda de fond invatat, nu deschidem. */
  if (g->inel_umplut < 2u)
  {
    g->c_on  = 0u;
    g->c_off = 0u;
    return false;
  }

  const float contrast = (g->nivel - g->fond) * SG_GATE_DB_PE_PAS;

  if (!g->activ)
  {
    const bool peste = (contrast >= SG_GATE_ON_DB) &&
                       (g->nivel  >= SG_GATE_NIVEL_MIN);
    if (peste)
    {
      g->c_on++;
      if (g->c_on >= SG_GATE_ON_CADRE)
      {
        g->activ   = true;
        g->front   = true;
        g->evenimente++;
        g->c_on    = 0u;
        g->c_off   = 0u;
        g->c_activ = 0u;
      }
    }
    else
    {
      g->c_on = 0u;
    }
  }
  else
  {
    g->c_activ++;

    if (contrast <= SG_GATE_OFF_DB)
    {
      g->c_off++;
      if (g->c_off >= SG_GATE_OFF_CADRE)
      {
        g->activ = false;
        g->c_off = 0u;
        /* Repornim invatarea fondului din starea curenta. */
        g->min_curent = g->nivel;
        g->sub_cadre  = 0u;
      }
    }
    else if (g->c_activ >= SG_GATE_MAX_CADRE)
    {
      /* Prea mult: nu e eveniment, e fond nou. Inchidem si reinvatam fondul
         de la nivelul de acum, altfel poarta ramane deschisa la infinit si
         clasificatorul ruleaza continuu. Golim tot inelul cu nivelul curent,
         ca sa nu mai existe un minim vechi si mic care sa redeschida imediat. */
      g->activ = false;
      g->c_off = 0u;
      g->fonduri_noi++;
      for (int i = 0; i < SG_GATE_SUB_NR; i++)
      {
        g->inel[i] = g->nivel;
      }
      g->inel_umplut = SG_GATE_SUB_NR;
      g->min_curent  = g->nivel;
      g->sub_cadre   = 0u;
    }
    else
    {
      g->c_off = 0u;
    }
  }

  return g->activ;
}

float sg_gate_nivel_db(const sg_gate_t *g)
{
  return g->nivel * SG_GATE_DB_PE_PAS;
}

float sg_gate_fond_db(const sg_gate_t *g)
{
  return g->fond * SG_GATE_DB_PE_PAS;
}

float sg_gate_contrast_db(const sg_gate_t *g)
{
  return (g->nivel - g->fond) * SG_GATE_DB_PE_PAS;
}
