/* SonicGrid - praguri de clasificare care se adapteaza la amplasament.
 *
 * Problema: pragurile fixe din sg_prag[] sunt reglate pe un set de date, nu pe
 * strada unde ajunge nodul. Intr-un loc mai zgomotos, modelul produce scoruri
 * mai mari pe toate clasele, fiindca semnalul vine mai slab si mai murdar, si
 * acelasi prag da mai multe alarme false.
 *
 * Ce NU face: nu se uita la nivelul de zgomot. Nivelul in dB nu spune cat de
 * mult se incurca modelul, si ca sa traduci "fond cu 12 dB mai mare" in "ridica
 * pragul cu 0.07" ai nevoie de o relatie diferita pe fiecare clasa si pe
 * fiecare amplasament.
 *
 * Ce face: se uita la scorurile pe care le produce modelul chiar acolo. Pentru
 * fiecare clasa urmareste mediana si o dispersie robusta, si pune pragul la
 *
 *     prag = mediana + K * dispersie
 *
 * Daca intr-un loc clasa sticla sta in medie la 0.3 in loc de 0.05, pragul urca
 * de la sine, oricati dB are fondul.
 *
 * ---------------------------------------------------------------- de ce cuantile
 *
 * Filtrul Kalman de pe nodul ESP32 autonom era potrivit acolo: urmarea fondul,
 * care e o marime de tip medie, cu inovatia limitata la 3 sigma ca sa nu fie
 * trasa de evenimente. Aici ne trebuie o cuantila din coada distributiei. O
 * medie ar fi trasa exact de varfurile pe care vrem sa le pastram, si ar trebui
 * sa le tai manual, adica sa faci cu mana ce face cuantila singura.
 *
 * Urmarirea e o aproximare stocastica, trei operatii pe clasa pe fereastra:
 *
 *     daca scor > q:  q += mu * p
 *     altfel:         q -= mu * (1 - p)
 *
 * La echilibru, fractiunea de scoruri de SUB q e exact p, adica q e cuantila p.
 * Cu p = 0.5 iese mediana, cu p = 0.84 iese mediana plus o sigma, deci
 * diferenta lor da dispersia. Doi float pe clasa, 12 in total.
 *
 * Semnele conteaza: cu pasii inversati, (1 - p) in sus si p in jos, converge la
 * cuantila 1 - p. Cu p = 0.5 cele doua variante sunt identice, deci greseala nu
 * se vede pe mediana.
 *
 * ------------------------------------------------------------- limite si siguranta
 *
 * Pragul adaptiv poate doar sa URCE peste cel din sg_prag[], niciodata sub.
 * Asta elimina singurul mod serios de a o da in bara: intr-un loc foarte
 * linistit toate scorurile stau aproape de zero, mediana plus K ori dispersia
 * ar cobori la 0.05, si nodul ar declansa pe nimic. Cu limita de jos luata din
 * masuratorile offline, cazul ala nu poate aparea. Iar daca locul redevine
 * linistit, estimarile scad si pragul se intoarce singur la valoarea de baza.
 *
 * Limita de sus exista ca un nod sa nu ramana surd definitiv dupa o seara
 * zgomotoasa.
 *
 * Primele SG_PA_MIN_N ferestre nu muta nimic: cu 10 masuratori, mediana si
 * dispersia sunt zgomot, si ar ridica pragul din intamplare.
 *
 * Se actualizeaza DOAR pe ferestrele in care poarta a fost deschisa. Alea sunt
 * ferestrele pe care se iau decizii, deci alea sunt distributia care conteaza.
 *
 * Starea nu supravietuieste unei reporniri. Dupa restart, nodul porneste de la
 * pragurile de baza si reinvata in cateva ore.
 *
 * ---------------------------------------------------------------- compromisul
 *
 * Adaptarea face nodul mai putin sensibil exact unde e mai zgomotos. Daca
 * zgomotul merge mana in mana cu riscul, pierzi tocmai acolo unde conta. E o
 * decizie de produs, nu una tehnica: K si SG_PA_MAX o controleaza.
 */
#ifndef SG_PRAG_ADAPTIV_H
#define SG_PRAG_ADAPTIV_H

#include <stdbool.h>
#include <stdint.h>

/* Cate dispersii peste mediana.
 *
 * Valoarea nu e aleasa din intuitie. Simulata pe scorurile reale ale modelului,
 * pe partitia de test trecuta prin canal:
 *
 *   K     ce se intampla
 *   2.0   patru din sase clase ramane la pragul de baza; adaptarea se
 *         declanseaza doar unde amplasamentul e chiar mai rau decat setul de
 *         test. Asta e ce vrem.
 *   2.5   sticla sare la plafon si acoperirea scade de la 33.6% la 7.1%, pe o
 *         distributie unde pragul de baza dadea doar 1.7% fals pozitiv.
 *   4.0   distruge sticla, grup si trafic.
 *
 * Deci 2.0, si pragul poate urca cel mult cu SG_PA_SALT_MAX peste baza. Fara
 * limita aia, o perioada zgomotoasa poate opri o clasa cu totul.
 */
#define SG_PA_K        2.0f
#define SG_PA_SALT_MAX 0.06f

/* Plafon absolut: peste atat clasa ar fi practic oprita. */
#define SG_PA_MAX      0.98f

/* Care clase se adapteaza, in ordinea din sg_clasa_t.
 *
 * grup si trafic sunt OPRITE intentionat. Ele sunt context, nu alarme: nu sunt
 * in SG_LEG_URGENTE si nu trezesc ESP-ul, iar pragurile lor de baza sunt mici
 * tocmai ca sa prinda situatia des. Simularea arata ca adaptarea le-ar ridica
 * pragul pana la plafon si acoperirea la trafic ar cadea de la 60.7% la 0.4%,
 * adica exact opusul rolului lor. */
#define SG_PA_ACTIVE   { true, true, true, true, false, false }

/* Cate ferestre inainte ca adaptarea sa aiba voie sa urce pragul. */
#define SG_PA_MIN_N    30u

/* Pasul de invatare: mare la inceput ca sa prinda repede amplasamentul, apoi
   din ce in ce mai mic, ca sa nu fie mutat de o singura seara. */
#define SG_PA_MU_START 0.05f
#define SG_PA_MU_MIN   0.002f
#define SG_PA_TAU_N    40.0f

void  sg_pa_init(void);

/* De chemat o data pe clasa, pentru fiecare fereastra in care poarta a fost
   deschisa si modelul a rulat. */
void  sg_pa_actualizeaza(int clasa, float scor);

/* Pragul de folosit acum pentru clasa. Niciodata sub sg_prag[clasa]. */
float sg_pa_curent(int clasa);

/* Cat s-a ridicat pragul peste cel de baza, in sutimi, pentru rapoarte si
   pentru cadrul de status catre ESP. 0 = nu s-a miscat. */
uint8_t sg_pa_deriva(int clasa);

/* Cate ferestre au intrat in estimare. */
uint32_t sg_pa_nr(int clasa);

/* Tipareste o linie cu starea tuturor claselor. */
void  sg_pa_raport(void);

#endif /* SG_PRAG_ADAPTIV_H */
