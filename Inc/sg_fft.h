/* FFT complex radix-2, in loc, pentru frontend-ul mel.

   Inlocuieste dsps_fft2r_fc32 din esp-dsp. Bufferul e intercalat
   (re, im, re, im, ...) exact ca la esp-dsp, ca sa nu schimbam
   structura codului portat.

   La 512 puncte si 160 MHz costa sub o milisecunda, iar un cadru
   apare la fiecare 20 ms, deci incarcarea e sub 5 la suta. */
#pragma once

#include <stdint.h>

#define SG_FFT_N 512

void sg_fft_init(void);
void sg_fft(float *buf);   /* buf are 2 * SG_FFT_N elemente */
