"""
Datele care apar in paginile legale (Termeni, Confidentialitate, Cookie-uri).

COMPLETEAZA toate campurile marcate cu [ ] inainte de a arata sistemul cuiva
din afara echipei. Pana atunci, paginile afiseaza vizibil ca sunt necompletate.

Cand schimbi ceva important in Termeni sau in Politica, creste TERMS_VERSION:
toti utilizatorii vor fi rugati sa accepte din nou la urmatoarea autentificare,
iar acceptarea se inregistreaza cu data si versiunea (dovada consimtamantului).
"""

LEGAL = {
    # ------------------------------------------------------------------ versiune
    "TERMS_VERSION": "2026-10-08",
    "ultima_actualizare": "8 octombrie 2026",

    # ------------------------------------------------------------------ operator
    # OPERATORUL DE DATE (GDPR art. 4 pct. 7) = cine decide DE CE si CUM se
    # prelucreaza datele.
    "operator": "Idor Bogdan Ioan",
    "operator_email": "idorbogdanioan@gmail.com",

    # ------------------------------------------------------------------ proiect
    "proiect": "SonicGrid",
    "descriere_scurta": "rețea de senzori acustici pentru detectarea evenimentelor sonore puternice",
    "locatie_monitorizata": "platoul Regie, București",
    "an_universitar": "2026-2027",

    # ------------------------------------------------------------------ temei legal
    # Temeiul pentru prelucrarea datelor persoanelor din zona monitorizata.
    # Discuta cu DPO-ul care se potriveste. In practica, pentru un sistem de
    # siguranta publica intr-un spatiu al universitatii, se invoca de obicei
    # interesul legitim (art. 6 alin. 1 lit. f) sau indeplinirea unei sarcini
    # de interes public (lit. e). Consimtamantul NU e realist: nu poti cere
    # acordul fiecarei persoane care trece prin zona.
    "temei_legal": "art. 6 alin. (1) lit. f) GDPR — interes legitim",
    "interes_legitim": "Detectarea timpurie a incidentelor care necesită intervenție, "
                       "pentru protejarea sănătății și siguranței persoanelor din zonă",

    # ------------------------------------------------------------------ ce prelucreaza
    # Tine-le sincronizate cu ce face EFECTIV codul. Daca schimbi sistemul,
    # schimba si aici - altfel politica devine falsa, ceea ce e mai rau decat
    # sa nu existe.
    "stocheaza_audio": False,      # nodurile NU trimit audio catre server
    "stocheaza_nivel": True,       # doar nivelul sonor in dBFS, la 5 secunde
    "clasificare_sunet": True,     # exista un clasificator, momentan nefunctional
    "recunoastere_vorbire": False, # NU rulam recunoastere de cuvinte
    "identifica_persoane": False,  # sistemul nu poate identifica pe nimeni
    "camere_video": False,         # nu exista nicio camera

    # ------------------------------------------------------------------ retentie
    # Acestea sunt aplicate EFECTIV de curatatorul din app.py. Daca le schimbi
    # aici, se schimba si stergerea automata.
    "retentie_masuratori_zile": 30,
    "retentie_alerte_zile": 90,
    "retentie_jurnal_acces_zile": 90,

    # ------------------------------------------------------------------ cookie-uri
    # Un singur cookie de sesiune, strict necesar pentru autentificare.
    # Directiva ePrivacy scuteste de consimtamant cookie-urile strict necesare,
    # deci NU e nevoie de banner de cookie-uri. Daca adaugi vreodata analytics
    # sau orice de la terti, situatia se schimba complet si ai nevoie de banner.
    "cookie_nume": "sg_session",
    "cookie_durata": "8 ore",
    "cookie_terti": False,
    "analytics": False,

    # ------------------------------------------------------------------ proprietate
    "licenta_cod": "toate drepturile rezervate",
    "detinator_drepturi": "Idor Bogdan Ioan",
    "an_copyright": "2026",
}


def campuri_necompletate():
    """Campurile ramase cu [ ]. Paginile le afiseaza ca avertisment vizibil,
    ca sa nu ajunga din greseala in fata cuiva."""
    # Un camp gol inseamna "nu se aplica" si pur si simplu nu apare in documente.
    # Doar cele ramase cu [ ] sunt nedecise.
    return [k for k, v in LEGAL.items()
            if isinstance(v, str) and v.strip().startswith("[")]
