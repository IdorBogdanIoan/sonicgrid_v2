/* Bufferul circular de cadre si declansarea deciziilor.

   Modelul se uita la o secunda de context si ia o decizie la fiecare 240 ms,
   conform device_spec.json (window_frames 50, hop_frames 12). Tinem ultimele
   50 de cadre normalizate si le livram in formatul asteptat de retea. */
#pragma once

#include <stdint.h>
#include <stdbool.h>

#include "mel_table.h"

/* Din device_spec.json. Daca le schimbi acolo, schimba-le si aici SI reantreneaza. */
#define SG_WIN_CADRE    50    /* 1.0 s de context */
/* 25 de cadre = 0.5 s intre decizii.

   Era 12, adica 0.24 s, dar masuratoarea pe placa a dat 191.6 ms per
   inferenta la 160 MHz. Cu pasul vechi procesorul ar fi fost ocupat 80%
   din timp cat poarta e deschisa. Cu 25 scade la 38%, iar latenta maxima
   urca la o jumatate de secunda, sub durata unui tipat. */
#define SG_WIN_PAS      25

typedef struct
{
  float    inel[SG_WIN_CADRE][SG_N_MELS];
  uint16_t idx;          /* urmatoarea pozitie de scris */
  uint16_t umplut;       /* cate cadre avem, pana la SG_WIN_CADRE */
  uint16_t de_la_ultima; /* cadre de la ultima fereastra livrata */
} sg_fereastra_t;

void sg_fer_init(sg_fereastra_t *f);
void sg_fer_push(sg_fereastra_t *f, const float *cadru);

/* True cand avem context complet si au trecut SG_WIN_PAS cadre de la ultima. */
bool sg_fer_gata(const sg_fereastra_t *f);

/* Scrie SG_N_MELS * SG_WIN_CADRE valori in ordine cronologica, asezate ca
   dest[banda * SG_WIN_CADRE + cadru], adica exact (1, 1, n_mels, T) pentru
   X-CUBE-AI. Reseteaza contorul de pas. */
void sg_fer_extrage(sg_fereastra_t *f, float *dest);
