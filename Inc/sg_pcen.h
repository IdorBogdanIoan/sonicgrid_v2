/* PCEN: normalizare adaptiva a castigului, in varianta cu stare.

   De ce PCEN si nu normalizare pe fereastra: microfonul tau la doi metri da
   -60 dBFS, iar fisierele din seturile publice sunt normalizate pe la -20.
   Castigul fix nu acopera diferenta, fiindca ai satura la surse apropiate.
   PCEN imparte fiecare banda la propriul fond neted, deci modelul se uita la
   FORMA evenimentului, nu la nivelul lui absolut. Un tipat la 5 metri intr-o
   hala si unul la 1 metru intr-o camera ajung sa semene.

   Portat din PacketFrontEnd.forward_stream (sonicgrid_scream/device_frontend.py).
   Formulele trebuie sa ramana identice cu cele din antrenare, altfel modelul
   primeste alte numere decat a invatat. */
#pragma once

#include <stdint.h>
#include <stdbool.h>

#include "mel_table.h"
#include "sg_pcen_params.h"

typedef struct
{
  float m[SG_N_MELS];   /* fondul neted, per banda */
  bool  pornit;         /* la primul cadru initializam m cu valoarea curenta */
} sg_pcen_t;

void sg_pcen_init(sg_pcen_t *p);

/* Consuma un cadru mel cuantizat si scrie SG_N_MELS valori normalizate. */
void sg_pcen_push(sg_pcen_t *p, const uint8_t *mel_q, float *iesire);
