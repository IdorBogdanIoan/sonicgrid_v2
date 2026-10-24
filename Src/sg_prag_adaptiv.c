/* SonicGrid - praguri de clasificare adaptive. Vezi sg_prag_adaptiv.h pentru
   motivele din spatele fiecarei alegeri. */

#include "sg_prag_adaptiv.h"

#include <math.h>
#include <stdio.h>

#include "sg_model.h"   /* sg_prag[] si sg_nume_clase[] */

typedef struct
{
  float    q50;   /* mediana scorului pe ferestrele cu poarta deschisa */
  float    q84;   /* mediana plus o sigma */
  uint32_t n;     /* cate ferestre au intrat in estimare */
} pa_clasa_t;

static pa_clasa_t s_pa[SG_NR_CLASE];
static const bool s_activ[SG_NR_CLASE] = SG_PA_ACTIVE;

/* Urmarire de cuantila prin aproximare stocastica. Nu are nevoie de istoric,
   doar de valoarea curenta.

   Deriva asteptata e mu * [ p*(1 - F(q)) - (1 - p)*F(q) ], care se anuleaza
   cand F(q) = p. Adica q se asaza exact acolo unde fractiunea de scoruri de
   SUB el e p, deci e cuantila p.

   Atentie la semne: varianta cu pasii inversati, (1-p) in sus si p in jos,
   converge la cuantila 1-p. Cu p = 0.5 cele doua variante sunt identice, deci
   eroarea nu se vede pe mediana si trece neobservata. Prima versiune a
   fisierului asta avea exact greseala aia si urmarea cuantila 0.16 in loc de
   0.84, deci dispersia iesea negativa si adaptarea nu facea nimic. */
static void urmareste(float *q, float x, float p, uint32_t n)
{
  float mu = SG_PA_MU_START / (1.0f + (float)n / SG_PA_TAU_N);
  if (mu < SG_PA_MU_MIN)
  {
    mu = SG_PA_MU_MIN;
  }

  if (x > *q)
  {
    *q += mu * p;
  }
  else
  {
    *q -= mu * (1.0f - p);
  }

  /* Scorurile sunt probabilitati, deci si cuantilele lor stau in [0, 1]. */
  if (*q < 0.0f) { *q = 0.0f; }
  if (*q > 1.0f) { *q = 1.0f; }
}

void sg_pa_init(void)
{
  for (int k = 0; k < SG_NR_CLASE; k++)
  {
    s_pa[k].q50 = 0.0f;
    s_pa[k].q84 = 0.0f;
    s_pa[k].n   = 0u;
  }
  printf("Praguri adaptive: K %ld/10, salt max %ld/100, dupa %lu ferestre | active:",
         (int32_t)(SG_PA_K * 10.0f), (int32_t)(SG_PA_SALT_MAX * 100.0f),
         (unsigned long)SG_PA_MIN_N);
  for (int k = 0; k < SG_NR_CLASE; k++)
  {
    if (s_activ[k])
    {
      printf(" %s", sg_nume_clase[k]);
    }
  }
  printf("\r\n");
}

void sg_pa_actualizeaza(int clasa, float scor)
{
  if ((clasa < 0) || (clasa >= SG_NR_CLASE))
  {
    return;
  }
  pa_clasa_t *c = &s_pa[clasa];

  urmareste(&c->q50, scor, 0.50f, c->n);
  urmareste(&c->q84, scor, 0.84f, c->n);

  /* Aici a fost o greseala care merita pastrata ca avertisment: fortam
     q84 >= q50 la fiecare pas. Pare inofensiv, dar amandoua pornesc de la zero
     si urca cu pasi diferiti: q50 cu +/- mu/2, iar q84 cu +0.16*mu in sus si
     -0.84*mu in jos, deci mult mai incet. In tot transitoriul q50 o ia inainte,
     limitarea trage q84 la valoarea lui, si de acolo cele doua rama n lipite
     pentru totdeauna. Dispersia iesea mereu zero si adaptarea nu facea nimic.
     Lasam cuantilele libere; semnul dispersiei se verifica la citire. */

  if (c->n < 0xFFFFFFFFu)
  {
    c->n++;
  }
}

float sg_pa_curent(int clasa)
{
  if ((clasa < 0) || (clasa >= SG_NR_CLASE))
  {
    return 1.0f;
  }
  const pa_clasa_t *c = &s_pa[clasa];
  const float baza = sg_prag[clasa];

  if (!s_activ[clasa])
  {
    return baza;
  }

  /* Pana se aduna destule masuratori, mediana si dispersia sunt zgomot si ar
     ridica pragul din intamplare. Folosim pragul din date. */
  if (c->n < SG_PA_MIN_N)
  {
    return baza;
  }

  /* In primele sute de ferestre q84 poate fi inca sub q50, fiindca urca mai
     incet. O dispersie negativa ar cobori pragul, deci o ducem la zero: in
     cazul ala pragul ramane cel de baza, ceea ce e exact ce vrem. */
  const float disp = (c->q84 > c->q50) ? (c->q84 - c->q50) : 0.0f;
  float prag = c->q50 + SG_PA_K * disp;

  /* Doar in sus. Vezi explicatia din header: altfel, intr-un loc linistit
     pragul s-ar prabusi si nodul ar declansa pe nimic. */
  if (prag < baza)
  {
    prag = baza;
  }
  /* Doua limite de sus: cat poate urca peste baza, si un plafon absolut. Prima
     opreste o seara zgomotoasa sa inchida clasa; a doua e plasa de siguranta
     daca pragul de baza e el insusi mare. */
  if (prag > baza + SG_PA_SALT_MAX)
  {
    prag = baza + SG_PA_SALT_MAX;
  }
  if (prag > SG_PA_MAX)
  {
    prag = SG_PA_MAX;
  }
  return prag;
}

uint8_t sg_pa_deriva(int clasa)
{
  if ((clasa < 0) || (clasa >= SG_NR_CLASE))
  {
    return 0u;
  }
  const float d = (sg_pa_curent(clasa) - sg_prag[clasa]) * 100.0f;
  if (d <= 0.0f)   { return 0u; }
  if (d >= 255.0f) { return 255u; }
  return (uint8_t)(d + 0.5f);
}

uint32_t sg_pa_nr(int clasa)
{
  if ((clasa < 0) || (clasa >= SG_NR_CLASE))
  {
    return 0u;
  }
  return s_pa[clasa].n;
}

void sg_pa_raport(void)
{
  printf("praguri ");
  for (int k = 0; k < SG_NR_CLASE; k++)
  {
    /* Tiparim in sutimi, ca intregi: printf cu virgula mobila e scump si
       pe unele configurari de linker nici nu e inclus. */
    printf("%s %ld", sg_nume_clase[k],
           (int32_t)(sg_pa_curent(k) * 100.0f + 0.5f));
    const uint8_t d = sg_pa_deriva(k);
    if (d != 0u)
    {
      printf("(+%u)", (unsigned)d);
    }
    printf(" ");
  }
  printf("| ferestre %lu\r\n", (unsigned long)s_pa[0].n);
}
