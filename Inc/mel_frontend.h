/* Frontend DSP pe nod: blocare componenta continua -> filtru trece-sus ->
   fereastra Hann -> FFT -> mel -> logaritm -> octet.

   Portat din esp32/main/mel_frontend.c. Singura schimbare fata de varianta
   de pe ESP32 e inlocuirea esp-dsp cu sg_fft. Restul e identic, inclusiv
   banca de filtre, ca iesirea sa ramana aceeasi cu DeviceFrontEnd din PyTorch. */
#pragma once

#include <stdint.h>
#include <stdbool.h>

#include "mel_table.h"

typedef struct
{
  /* stare filtru biquad trece-sus */
  float x1, x2, y1, y2;
  float b0, b1, b2, a1, a2;

  /* blocare componenta continua, un singur pol */
  float dc_x1, dc_y1;

  /* suprapunere: pastram SG_FRAME_LEN - SG_HOP_LEN esantioane intre apeluri */
  float overlap[SG_FRAME_LEN];
  int   overlap_len;

  bool  clipped;
  float sumsq;
  int   nsamp;
} sg_frontend_t;

void  sg_frontend_init(sg_frontend_t *fe);
int   sg_frontend_process(sg_frontend_t *fe, const int16_t *pcm, int n, uint8_t *out);
float sg_frontend_take_rms_dbfs(sg_frontend_t *fe);
bool  sg_frontend_take_clipped(sg_frontend_t *fe);
