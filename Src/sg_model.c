#include "sg_model.h"

#include <math.h>
#include <stdio.h>
#include <string.h>

#include "main.h"
#include "network.h"
#include "stai.h"

/* Factorii de cuantizare vin DIRECT din network.h, nu copiati cu mana din
   raport.

   Varianta cu numere scrise aici a fost o capcana reala. Se schimba la fiecare
   reexport, iar daca uiti sa le actualizezi nu apare nicio eroare: modelul pare
   doar prost. La ultimul reexport, scala de iesire a trecut de la 0.052343 la
   0.104955 si zero-ul de la 104 la 59. Un model care decuantizeaza cu jumatate
   din scala corecta da logiti la jumatate, deci probabilitati trase spre 0.5,
   si ai fi cautat vina in antrenare.

   Acum nu mai pot fi desincronizate. */
#if !defined(STAI_NETWORK_IN_1_SCALE) || !defined(STAI_NETWORK_OUT_1_SCALE)
#error "network.h nu expune factorii de cuantizare. Verifica ce a generat ST Edge AI."
#endif

#define SG_Q_IN_SCALA    (STAI_NETWORK_IN_1_SCALE)
#define SG_Q_IN_ZERO     (STAI_NETWORK_IN_1_ZERO_POINT)
#define SG_Q_OUT_SCALA   (STAI_NETWORK_OUT_1_SCALE)
#define SG_Q_OUT_ZERO    (STAI_NETWORK_OUT_1_ZERO_POINT)

/* Doua verificari la compilare, ca o nepotrivire sa dea eroare de build in loc
   de rezultate aiurea. Prima prinde o fereastra de alta dimensiune, a doua un
   model exportat cu alt numar de clase. */
_Static_assert(STAI_NETWORK_IN_1_SIZE == SG_MODEL_INTRARE,
               "network.h si SG_MODEL_INTRARE nu se potrivesc");
_Static_assert(STAI_NETWORK_OUT_1_SIZE == SG_NR_CLASE,
               "modelul generat nu are SG_NR_CLASE iesiri");

const char *const sg_nume_clase[SG_NR_CLASE] = {
  "tipat", "sticla", "claxon", "alarma", "grup", "trafic"
};

/* Pragurile de BAZA. Praguri adaptive (sg_prag_adaptiv.c) pot doar sa urce
   peste ele, niciodata sa coboare sub.

   Valorile vin din masuratori, nu din burta: antrenare/praguri.py le calculeaza
   pe partitia de test trecuta prin canalul simulat, cautand prima valoare la
   care precizia trece de 0.5 cu o acoperire de cel putin 0.2. Unde nu exista
   nicio asemenea valoare, se cere incredere mare, ca sa nu se inunde gateway-ul.

   grup si trafic au praguri mici fiindca nu sunt alarme: nu sunt in
   SG_LEG_URGENTE, deci nu trezesc ESP-ul. Conteaza doar pentru clasa dominanta
   din cadrul de status. */
const float sg_prag[SG_NR_CLASE] = {
  0.80f,   /* tipat  */
  0.90f,   /* sticla */
  0.90f,   /* claxon, cel mai slab: AUC 0.73 */
  0.90f,   /* alarma */
  0.30f,   /* grup,   context, nu alarma */
  0.50f,   /* trafic, context, nu alarma */
};

STAI_NETWORK_CONTEXT_DECLARE(s_ctx, STAI_NETWORK_CONTEXT_SIZE)

STAI_ALIGNED(32)
static uint8_t s_activari[STAI_NETWORK_ACTIVATION_1_SIZE_BYTES];
static stai_ptr s_lista_activari[] = { s_activari };

static stai_ptr s_intrari[STAI_NETWORK_IN_NUM];
static stai_ptr s_iesiri[STAI_NETWORK_OUT_NUM];
static bool     s_gata = false;
static uint32_t s_durata_ms = 0u;

bool sg_model_init(void)
{
  if (stai_runtime_init() != STAI_SUCCESS)                      { return false; }
  if (stai_network_init(s_ctx) != STAI_SUCCESS)                 { return false; }
  if (stai_network_set_activations(s_ctx, s_lista_activari,
        STAI_NETWORK_ACTIVATIONS_NUM) != STAI_SUCCESS)          { return false; }

  stai_size n = 0u;
  if (stai_network_get_inputs(s_ctx, s_intrari, &n) != STAI_SUCCESS ||
      n != STAI_NETWORK_IN_NUM)                                 { return false; }
  if (stai_network_get_outputs(s_ctx, s_iesiri, &n) != STAI_SUCCESS ||
      n != STAI_NETWORK_OUT_NUM)                                { return false; }

  /* Tiparim ce a generat exportul, ca o nepotrivire sa se vada din prima linie
     de pe serial. Scalele le dam inmultite cu un milion, ca sa nu depindem de
     printf cu virgula mobila. */
  printf("Model: intrare %d, iesire %d, activari %d B, greutati %d B\r\n",
         (int)STAI_NETWORK_IN_1_SIZE, (int)STAI_NETWORK_OUT_1_SIZE,
         (int)STAI_NETWORK_ACTIVATION_1_SIZE_BYTES,
         (int)STAI_NETWORK_WEIGHTS_SIZE);
  printf("  cuantizare: intrare %ld e-6 zero %d | iesire %ld e-6 zero %d\r\n",
         (int32_t)(SG_Q_IN_SCALA * 1000000.0f), (int)SG_Q_IN_ZERO,
         (int32_t)(SG_Q_OUT_SCALA * 1000000.0f), (int)SG_Q_OUT_ZERO);

  s_gata = true;
  return true;
}

bool sg_model_ruleaza(const float *fereastra, float *probabilitati)
{
  if (!s_gata) { return false; }

  int8_t *in = (int8_t *)s_intrari[0];
  for (int i = 0; i < SG_MODEL_INTRARE; i++)
  {
    int32_t q = (int32_t)lrintf(fereastra[i] / SG_Q_IN_SCALA) + SG_Q_IN_ZERO;
    if (q < -128) { q = -128; }
    if (q >  127) { q =  127; }
    in[i] = (int8_t)q;
  }

  const uint32_t t0 = HAL_GetTick();
  const stai_return_code rc = stai_network_run(s_ctx, STAI_MODE_SYNC);
  s_durata_ms = HAL_GetTick() - t0;
  if (rc != STAI_SUCCESS) { return false; }

  const int8_t *out = (const int8_t *)s_iesiri[0];
  for (int k = 0; k < SG_NR_CLASE; k++)
  {
    const float logit = ((float)out[k] - (float)SG_Q_OUT_ZERO) * SG_Q_OUT_SCALA;
    probabilitati[k] = 1.0f / (1.0f + expf(-logit));
  }
  return true;
}

uint32_t sg_model_durata_ms(void) { return s_durata_ms; }
