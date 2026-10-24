"""Date pentru paginile legale. COMPLETEAZA campurile marcate cu [ ] inainte de lansare.

Cand modifici substantial Termenii sau Politica, creste TERMS_VERSION:
toti utilizatorii vor fi rugati sa accepte din nou la urmatorul login.
"""
LEGAL = {
    "TERMS_VERSION": "2026-09-21",
    "last_updated": "21 septembrie 2026",

    # Operatorul datelor (GDPR art. 4 pct. 7). Pentru un proiect UPB, de regula
    # universitatea / facultatea e operatorul, nu studentii. Verifica cu profesorul.
    "operator": "[Universitatea Națională de Știință și Tehnologie POLITEHNICA București — Facultatea ...]",
    "operator_address": "[Splaiul Independenței nr. 313, sector 6, București]",
    "project": "SonicGrid",
    "contact_email": "[email de contact al proiectului]",
    "dpo_email": "[dpo@upb.ro — verifică adresa oficială a responsabilului cu protecția datelor]",
    "location": "[locul monitorizat, ex: zona festivalului X / clădirea Rectorat, corp AN]",
    "coordinator": "[Nume profesor coordonator]",

    # Retentie (ajusteaza la ce face efectiv serverul).
    "retention_events_days": 30,
    "retention_logs_days": 30,

    # Ce face EFECTIV serverul. Textul din /confidentialitate se schimba dupa astea,
    # deci tine-le sincronizate cu codul (server.py).
    "keyword_spotting": True,     # Vosk cauta "ajutor", "help"... in fragmentele audio
    "stores_audio_clips": True,   # fragmentele cu cuvant detectat se salveaza in alerte/
    "retention_audio_days": 14,
}
