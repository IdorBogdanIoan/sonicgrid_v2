/* Clasificatorul, invelis peste codul generat de ST Edge AI.

   Sase iesiri independente, nu una. Fiecare are propria probabilitate si pot
   fi active simultan: un tipat se poate intampla in mijlocul traficului.
   De-aia sigmoida pe fiecare iesire, nu softmax peste toate. */
#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "mel_table.h"
#include "sg_fereastra.h"

#define SG_MODEL_INTRARE   (SG_N_MELS * SG_WIN_CADRE)
#define SG_NR_CLASE        6

/* Ordinea e cea din clase.py, nu o schimba fara sa reexporti modelul. */
typedef enum {
  SG_TIPAT = 0, SG_STICLA, SG_CLAXON, SG_ALARMA, SG_GRUP, SG_TRAFIC
} sg_clasa_t;

extern const char *const sg_nume_clase[SG_NR_CLASE];

/* Praguri pe clasa, in sutimi. Trebuie reglate pe nodul real, la distanta
   de montare. Valorile de start sunt conservatoare.

   Clasele care au iesit bine la antrenare, corelatie cu profesorul:
     grup   0.95    tipat  0.82    claxon 0.70
   Cele care NU au iesit:
     sticla 0.15    alarma 0.17
   Pe ultimele doua nu te baza pana nu aduni mai multe inregistrari. */
extern const float sg_prag[SG_NR_CLASE];

bool     sg_model_init(void);
bool     sg_model_ruleaza(const float *fereastra, float *probabilitati);
uint32_t sg_model_durata_ms(void);
