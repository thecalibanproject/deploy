"""The synthetic practice database of Hadad & Associés (MongoDB database `lawfirm`).

Everything is invented: the firm, its lawyers, its clients, the opposing parties, the matters, the
amounts, the e-mail domains (all under `.example`) and the identifiers. Real place names only
(Beirut, Paris and their districts, the courts' kinds). Dates are relative to the moment the seed
runs, so "upcoming" deadlines stay upcoming whenever the demo is recorded.

Three collections:

- clients       personal data on purpose: names, e-mail addresses, French and Lebanese phone
                numbers, a Lebanese-style national ID (synthetic pattern LB-dddd-dddd-d, the
                tenant's own PII pattern) and, for the Paris individual client, a French NIR whose
                key is valid (so the gateway's built-in detector recognises it)
- matters       one document per matter: type, status, stage, court, office, responsible lawyer,
                client reference, embedded `deadlines` and `parties` (the conflict check reads them)
- time_entries  billable time per matter and lawyer, billed or not
"""
from datetime import datetime, timedelta, timezone

LAWYERS = {
    "NH": {"name": "Nour Azoury", "office": "beirut", "role": "managing partner", "rate": 420, "currency": "USD"},
    "LS": {"name": "Lina Sarkis", "office": "beirut", "role": "senior associate", "rate": 260, "currency": "USD"},
    "JM": {"name": "Julien Marchetti", "office": "paris", "role": "partner", "rate": 480, "currency": "EUR"},
    "CR": {"name": "Camille Rousseau", "office": "paris", "role": "associate", "rate": 290, "currency": "EUR"},
}

CLIENTS = [
    {"_id": "CLI-001", "type": "individual", "name": "Elias Khoury", "office": "beirut", "language": "fr",
     "email": "elias.khoury@mail-demo.example", "phone": "+961 3 412 785", "national_id": "LB-5180-2241-7",
     "kyc_status": "verified", "client_since_days": -1460},
    {"_id": "CLI-002", "type": "company", "name": "Levant Agro SAL", "office": "beirut", "language": "en",
     "email": "legal@levant-agro.example", "phone": "+961 1 334 210", "contact_person": "Maya Azar",
     "kyc_status": "verified", "client_since_days": -2190},
    {"_id": "CLI-003", "type": "company", "name": "Maison Delorme SAS", "office": "paris", "language": "fr",
     "email": "juridique@maison-delorme.example", "phone": "+33 1 44 55 21 90", "contact_person": "Hélène Delorme",
     "kyc_status": "verified", "client_since_days": -900},
    {"_id": "CLI-004", "type": "individual", "name": "Sophie Martin-Aziz", "office": "paris", "language": "fr",
     "email": "sophie.martinaziz@mail-demo.example", "phone": "+33 6 41 27 88 30", "nir": "2 89 04 75 114 052 97",
     "kyc_status": "verified", "client_since_days": -120},
    {"_id": "CLI-005", "type": "company", "name": "Cedar Logistics SARL", "office": "beirut", "language": "en",
     "email": "ops@cedar-logistics.example", "phone": "+961 1 990 412", "contact_person": "Fadi Tannous",
     "kyc_status": "verified", "client_since_days": -1100},
    {"_id": "CLI-006", "type": "individual", "name": "Karim Nassar", "office": "beirut", "language": "en",
     "email": "karim.nassar@mail-demo.example", "phone": "+961 70 218 904", "national_id": "LB-6620-1457-2",
     "kyc_status": "pending", "client_since_days": -30},
]


def _dl(kind, label, days, status="open", court=None):
    return {"kind": kind, "label": label, "due_in_days": days, "status": status, "court": court}


def _party(name, role, kind):
    return {"name": name, "role": role, "kind": kind}


MATTERS = [
    {"_id": "HAD-2026-014", "title": "Succession Khoury : partage d'un immeuble à Achrafieh", "type": "inheritance",
     "status": "open", "stage": "expertise judiciaire", "office": "beirut", "lawyer": "NH", "client_id": "CLI-001",
     "jurisdiction": "Tribunal de première instance de Beyrouth", "fee_arrangement": "hourly", "opened_days": -310,
     "deadlines": [
         _dl("filing", "Communication des pièces à l'expert", -12, "done", "Tribunal de première instance de Beyrouth"),
         _dl("filing", "Dépôt des observations sur le rapport d'expertise", 9, court="Tribunal de première instance de Beyrouth"),
         _dl("hearing", "Audience de plaidoiries", 23, court="Tribunal de première instance de Beyrouth"),
         _dl("tax", "Déclaration et paiement des droits de succession", 41),
     ],
     "parties": [_party("Elias Khoury", "client", "person"), _party("Georges Khoury", "adverse", "person"),
                 _party("Rita Saadé", "third_party", "person")]},
    {"_id": "HAD-2025-088", "title": "Khoury : recouvrement de loyers, local commercial de Mar Mikhaël",
     "type": "real_estate", "status": "on_hold", "stage": "négociation amiable", "office": "beirut", "lawyer": "LS",
     "client_id": "CLI-001", "jurisdiction": "Juge des référés de Beyrouth", "fee_arrangement": "fixed",
     "opened_days": -400,
     "deadlines": [_dl("notice", "Fin du délai de grâce accordé au locataire", 60)],
     "parties": [_party("Elias Khoury", "client", "person"), _party("Café Saint-Nicolas SARL", "adverse", "company")]},
    {"_id": "HAD-2026-021", "title": "Levant Agro SAL c. Medco Trading SAL : rupture du contrat de distribution",
     "type": "commercial_litigation", "status": "open", "stage": "mise en état", "office": "beirut", "lawyer": "LS",
     "client_id": "CLI-002", "jurisdiction": "Tribunal de commerce de Beyrouth", "fee_arrangement": "hourly",
     "opened_days": -150,
     "deadlines": [
         _dl("letter", "Réponse à la mise en demeure de Medco Trading", 5),
         _dl("hearing", "Audience de mise en état", 30, court="Tribunal de commerce de Beyrouth"),
     ],
     "parties": [_party("Levant Agro SAL", "client", "company"), _party("Medco Trading SAL", "adverse", "company")]},
    {"_id": "HAD-2024-051", "title": "Levant Agro SAL : dépôt de la marque CEDARFIELD", "type": "intellectual_property",
     "status": "closed", "stage": "clos", "office": "beirut", "lawyer": "NH", "client_id": "CLI-002",
     "jurisdiction": "Office de la propriété intellectuelle (Liban)", "fee_arrangement": "fixed", "opened_days": -800,
     "deadlines": [_dl("filing", "Dépôt de la demande d'enregistrement", -700, "done")],
     "parties": [_party("Levant Agro SAL", "client", "company")]},
    {"_id": "HAD-2026-007", "title": "Maison Delorme SAS : renouvellement du bail commercial, boutique rue de Turenne",
     "type": "real_estate", "status": "open", "stage": "procédure au fond", "office": "paris", "lawyer": "JM",
     "client_id": "CLI-003", "jurisdiction": "Tribunal judiciaire de Paris", "fee_arrangement": "hourly",
     "opened_days": -200,
     "deadlines": [
         _dl("filing", "Conclusions en réponse", 16, court="Tribunal judiciaire de Paris"),
         _dl("hearing", "Audience de plaidoiries", 72, court="Tribunal judiciaire de Paris"),
     ],
     "parties": [_party("Maison Delorme SAS", "client", "company"), _party("SCI Rivoli Invest", "adverse", "company")]},
    {"_id": "HAD-2026-033", "title": "Sophie Martin-Aziz c. Nexora Conseil SAS : contestation d'un licenciement",
     "type": "employment", "status": "open", "stage": "conciliation", "office": "paris", "lawyer": "CR",
     "client_id": "CLI-004", "jurisdiction": "Conseil de prud'hommes de Paris", "fee_arrangement": "hourly",
     "opened_days": -60,
     "deadlines": [
         _dl("filing", "Communication des pièces à la partie adverse", 3, court="Conseil de prud'hommes de Paris"),
         _dl("hearing", "Audience de conciliation et d'orientation", 14, court="Conseil de prud'hommes de Paris"),
     ],
     "parties": [_party("Sophie Martin-Aziz", "client", "person"), _party("Nexora Conseil SAS", "adverse", "company")]},
    {"_id": "HAD-2025-112", "title": "Cedar Logistics SARL c. Groupe Valmont SA : arbitrage CCI (siège Paris)",
     "type": "arbitration", "status": "open", "stage": "échange des mémoires", "office": "paris", "lawyer": "JM",
     "client_id": "CLI-005", "jurisdiction": "Arbitrage CCI, siège Paris", "fee_arrangement": "capped",
     "opened_days": -420,
     "deadlines": [
         _dl("filing", "Mémoire en réplique (Statement of Reply)", 18, court="Tribunal arbitral CCI"),
         _dl("hearing", "Audience sur le fond (Paris)", 47, court="Tribunal arbitral CCI"),
     ],
     "parties": [_party("Cedar Logistics SARL", "client", "company"), _party("Groupe Valmont SA", "adverse", "company")]},
    {"_id": "HAD-2026-040", "title": "Karim Nassar : clause de non-concurrence, contrat de travail Bayt Tech SAL",
     "type": "employment", "status": "open", "stage": "consultation", "office": "beirut", "lawyer": "LS",
     "client_id": "CLI-006", "jurisdiction": "Conseil (pas de procédure)", "fee_arrangement": "fixed",
     "opened_days": -20,
     "deadlines": [_dl("advice", "Avis écrit sur la clause de non-concurrence", 11)],
     "parties": [_party("Karim Nassar", "client", "person"), _party("Bayt Tech SAL", "counterparty", "company")]},
]

# Time entries: (matter, lawyer, days ago, hours, activity, billed)
ENTRIES = [
    ("HAD-2026-014", "NH", 300, 2.5, "Premier rendez-vous et analyse de l'acte de notoriété", True),
    ("HAD-2026-014", "NH", 240, 4.0, "Requête en partage judiciaire", True),
    ("HAD-2026-014", "LS", 180, 6.5, "Recherches sur l'évaluation des biens indivis", True),
    ("HAD-2026-014", "NH", 60, 3.0, "Réunion d'expertise sur place", False),
    ("HAD-2026-014", "LS", 20, 5.0, "Projet d'observations sur le pré-rapport", False),
    ("HAD-2025-088", "LS", 380, 1.5, "Mise en demeure du locataire", True),
    ("HAD-2025-088", "LS", 200, 2.0, "Négociation d'un échéancier", True),
    ("HAD-2026-021", "LS", 140, 3.5, "Analyse du contrat de distribution exclusive", True),
    ("HAD-2026-021", "NH", 120, 2.0, "Stratégie contentieuse avec le client", True),
    ("HAD-2026-021", "LS", 90, 7.5, "Assignation devant le tribunal de commerce", True),
    ("HAD-2026-021", "LS", 35, 4.0, "Calcul du préjudice et pièces comptables", False),
    ("HAD-2026-021", "NH", 12, 1.5, "Appel avec le conseil de Medco Trading", False),
    ("HAD-2026-021", "LS", 4, 2.5, "Projet de réponse à la mise en demeure", False),
    ("HAD-2024-051", "NH", 760, 3.0, "Recherche d'antériorités et dépôt", True),
    ("HAD-2026-007", "JM", 190, 2.0, "Analyse du bail et du congé avec offre de renouvellement", True),
    ("HAD-2026-007", "CR", 150, 6.0, "Assignation en fixation du loyer renouvelé", True),
    ("HAD-2026-007", "JM", 45, 2.5, "Rendez-vous avec l'expert immobilier", False),
    ("HAD-2026-007", "CR", 10, 5.5, "Projet de conclusions en réponse", False),
    ("HAD-2026-033", "CR", 55, 3.0, "Entretien avec la cliente et chronologie", True),
    ("HAD-2026-033", "CR", 25, 4.5, "Saisine du conseil de prud'hommes", False),
    ("HAD-2026-033", "JM", 8, 1.0, "Relecture de la requête", False),
    ("HAD-2025-112", "JM", 400, 8.0, "Requête d'arbitrage", True),
    ("HAD-2025-112", "CR", 330, 12.0, "Mémoire en demande", True),
    ("HAD-2025-112", "JM", 160, 6.0, "Audience de procédure (case management conference)", True),
    ("HAD-2025-112", "CR", 30, 9.5, "Projet de mémoire en réplique", False),
    ("HAD-2025-112", "JM", 6, 3.5, "Préparation des témoins", False),
    ("HAD-2026-040", "LS", 15, 2.0, "Lecture du contrat de travail et de la clause", False),
]


def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def documents(now=None):
    """The three collections as plain JSON (timestamps as ISO 8601 strings, UTC; the seed turns
    them into BSON dates)."""
    now = (now or datetime.now(timezone.utc)).replace(hour=9, minute=0, second=0, microsecond=0)
    clients = []
    for c in CLIENTS:
        d = {k: v for k, v in c.items() if k != "client_since_days"}
        d["client_since"] = iso(now + timedelta(days=c["client_since_days"]))
        clients.append(d)
    matters = []
    for m in MATTERS:
        lw = LAWYERS[m["lawyer"]]
        d = {k: v for k, v in m.items() if k not in ("lawyer", "opened_days", "deadlines")}
        d["responsible_lawyer"] = lw["name"]
        d["opened_at"] = iso(now + timedelta(days=m["opened_days"]))
        d["deadlines"] = [{"kind": x["kind"], "label": x["label"], "status": x["status"], "court": x["court"],
                           "due_at": iso(now + timedelta(days=x["due_in_days"]))} for x in m["deadlines"]]
        matters.append(d)
    entries = []
    for i, (matter, who, ago, hours, activity, billed) in enumerate(ENTRIES, start=1):
        lw = LAWYERS[who]
        entries.append({"_id": f"TE-{i:04d}", "matter_id": matter, "lawyer": lw["name"], "office": lw["office"],
                        "worked_on": iso(now - timedelta(days=ago)), "hours": hours, "rate": lw["rate"],
                        "amount": round(hours * lw["rate"], 2), "currency": lw["currency"], "activity": activity,
                        "billed": billed})
    return {"clients": clients, "matters": matters, "time_entries": entries}


def person_terms():
    """Whole-word terms of the tenant's PII dictionary: every first and family name of the people
    in the database (clients, parties, contacts, lawyers), one term each, so that a family name
    alone ("le dossier Khoury") and a full name get consistent surrogates. Companies are not
    personal data and stay as they are."""
    names = set()
    for c in CLIENTS:
        if c["type"] == "individual":
            names.add(c["name"])
        if c.get("contact_person"):
            names.add(c["contact_person"])
    for m in MATTERS:
        for p in m["parties"]:
            if p["kind"] == "person":
                names.add(p["name"])
    for lw in LAWYERS.values():
        names.add(lw["name"])
    terms = set()
    for n in names:
        for part in n.replace("-", " ").split():
            if len(part) >= 2:
                terms.add(part)
        if "-" in n:
            terms.add(n.split()[-1])
    return sorted(terms)


if __name__ == "__main__":
    import json
    print(json.dumps({"documents": documents(), "person_terms": person_terms()}, indent=2, ensure_ascii=False))
