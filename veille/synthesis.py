"""Synthèse IA (Claude) d'un appel d'offres : points majeurs, lots, risques, estimation.

Traite les fiches dont `synthese_statut == "demandee"` :
  - automatiquement pour les avis très pertinents (synthèse de l'avis seul) ;
  - à la demande de l'équipe depuis le site, après dépôt du DCE.

Usage : python -m veille.synthesis [--local] [--max 10]
"""
from __future__ import annotations

import argparse
import base64
import json
import logging
import os

import yaml

from .documents import Piece, assembler, extraire
from .store import Store, ouvrir
from .util import PARIS, RACINE, charger_profil, maintenant, parser_date

log = logging.getLogger("veille.synthesis")

# Une variable GitHub absente arrive vide : « or » applique la valeur par défaut
MODELE = (os.environ.get("CLAUDE_MODEL") or "").strip() or "claude-sonnet-5-5"

SCHEMA = {
    "type": "object",
    "properties": {
        "resume": {"type": "string", "description": "Synthèse en 4 à 6 phrases : quoi, où, pour qui, quand, combien."},
        "points_majeurs": {"type": "array", "items": {"type": "string"}, "description": "5 à 10 points clés pour décider."},
        "lots": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "numero": {"type": "string"},
                    "intitule": {"type": "string"},
                    "pertinence_adr": {"type": "string", "enum": ["forte", "moyenne", "faible", "nulle"]},
                    "commentaire": {"type": "string"},
                },
                "required": ["intitule", "pertinence_adr"],
            },
        },
        "date_limite": {"type": ["string", "null"], "description": "Date et heure limite de remise (ISO 8601) si trouvée."},
        "visite": {
            "type": "object",
            "properties": {
                "obligatoire": {"type": ["boolean", "null"]},
                "details": {"type": "string", "description": "Dates, modalités, contact pour la visite."},
            },
        },
        "pieces_a_fournir": {"type": "array", "items": {"type": "string"}},
        "criteres_attribution": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"critere": {"type": "string"}, "poids": {"type": "string"}},
                "required": ["critere"],
            },
        },
        "exigences_techniques": {"type": "array", "items": {"type": "string"}, "description": "Matériaux, finitions, normes (feu M1/B-s1-d0, PMR…), échantillons, qualifications demandées."},
        "planning": {"type": "string", "description": "Délais d'exécution, période de travaux, contraintes d'accès."},
        "risques": {"type": "array", "items": {"type": "string"}, "description": "Pénalités, délais serrés, exigences inhabituelles, assurances, avances…"},
        "questions_acheteur": {"type": "array", "items": {"type": "string"}, "description": "Questions à poser à l'acheteur avant la date limite."},
        "estimation_cout": {
            "type": "object",
            "properties": {
                "montant_min_ht": {"type": ["number", "null"]},
                "montant_max_ht": {"type": ["number", "null"]},
                "perimetre": {"type": "string", "description": "Sur quels lots / ouvrages porte l'estimation."},
                "base": {"type": "string", "description": "Montant de l'avis, DPGF, ratios du référentiel ADR, ordres de grandeur…"},
                "hypotheses": {"type": "array", "items": {"type": "string"}},
                "fiabilite": {"type": "string", "enum": ["faible", "moyenne", "bonne"]},
            },
            "required": ["perimetre", "base", "fiabilite"],
        },
        "categorie_recommandee": {"type": "string", "enum": ["directe", "groupement", "sous_traitance", "non_pertinente"]},
        "recommandation": {"type": "string", "enum": ["go", "a_etudier", "no_go"]},
        "justification": {"type": "string", "description": "Pourquoi cette recommandation, en 2 à 4 phrases."},
        "informations_manquantes": {"type": "array", "items": {"type": "string"}, "description": "Ce qui n'a pas pu être vérifié faute de pièces."},
    },
    "required": ["resume", "points_majeurs", "estimation_cout", "categorie_recommandee", "recommandation", "justification"],
}

SYSTEME = """Tu es chargé d'études de prix et responsable des appels d'offres chez L'Atelier du Renard (ADR),
entreprise francilienne de menuiserie intérieure et extérieure bois, agencement, mobilier sur mesure,
scénographie, muséographie, stands et PLV. ADR intervient sur des commerces, hôtels, bureaux,
établissements scolaires, culturels et sportifs (espaces VIP, loges, hospitalités, bars, banques d'accueil).

Ta mission : analyser un appel d'offres et produire une fiche de décision fiable pour l'équipe.

Règles impératives :
- N'invente rien. Chaque date, montant, exigence ou critère doit provenir des pièces fournies.
  Si une information n'y figure pas, laisse le champ vide et ajoute-la à « informations_manquantes ».
- Distingue réponse directe (lot menuiserie/agencement séparé que ADR peut porter seule),
  groupement (marché global ou multi-métiers où ADR doit s'associer), sous-traitance
  (marché tous corps d'état / entreprise générale, ou marché déjà attribué) et non pertinent.
- Pour l'estimation de coût : appuie-toi d'abord sur le montant estimé de l'avis ou la DPGF/BPU,
  puis sur le référentiel de prix ADR s'il est renseigné, sinon sur des ordres de grandeur
  du marché francilien. Donne une fourchette HT, le périmètre, la base et les hypothèses ;
  la fiabilité est « faible » si tu n'as ni quantitatif ni montant de référence.
- Écris en français, de façon concise et concrète, pour des professionnels du bâtiment."""


def _referentiel(store: Store | None = None) -> str:
    """Prix ADR : réglés dans le site (page Réglages), à défaut config/referentiel_prix.yaml."""
    data = store.parametre("referentiel_prix") if store else None
    if not isinstance(data, dict):
        chemin = RACINE / "config" / "referentiel_prix.yaml"
        data = yaml.safe_load(chemin.read_text(encoding="utf-8")) if chemin.exists() else {}
        data = data or {}
    ouvrages = data.get("ouvrages") or []
    if isinstance(ouvrages, dict):  # format du fichier YAML
        ouvrages = [{"nom": k, **(v or {})} for k, v in ouvrages.items()]
    ouvrages = [o for o in ouvrages if isinstance(o, dict) and (o.get("prix_min") or o.get("prix_max"))]
    taux = {k: data.get(k) for k in ("taux_horaire_atelier", "taux_horaire_pose", "coefficient_frais_generaux", "marge_cible") if data.get(k)}
    if not ouvrages and not taux:
        return "(non renseigné par l'équipe)"
    return yaml.safe_dump({**taux, "ouvrages": ouvrages, "remarques": data.get("remarques") or ""},
                          allow_unicode=True, sort_keys=False)


def _entete(ao: dict) -> str:
    def d(v):
        x = parser_date(v)
        return x.astimezone(PARIS).strftime("%d/%m/%Y %H:%M") if x else "non indiquée"

    return (
        f"Source : {ao.get('source')} — réf. {ao.get('ref')} — {ao.get('url')}\n"
        f"Objet : {ao.get('titre')}\nAcheteur : {ao.get('acheteur')}\nLieu : {ao.get('lieu')}\n"
        f"Nature : {ao.get('nature')} — Procédure : {ao.get('procedure')}\n"
        f"Type de marché : {', '.join(ao.get('type_marche') or [])}\nCPV : {', '.join(ao.get('cpv') or [])}\n"
        f"Date de publication : {d(ao.get('date_publication'))} — Date limite : {d(ao.get('date_limite'))}\n"
        f"Score de pertinence automatique : {ao.get('score')} ({'; '.join(ao.get('score_detail') or [])})\n"
    )


def preparer(ao: dict, store: Store) -> tuple[list[dict], list[str]]:
    """Construit le message pour Claude : en-tête de l'avis + DCE déposés."""
    pieces: list[Piece] = []
    for doc in ao.get("documents") or []:
        try:
            contenu = store.telecharger(doc["chemin"])
            pieces += extraire(doc.get("nom") or doc["chemin"], contenu)
        except Exception as e:
            log.warning("Téléchargement %s : %s", doc.get("chemin"), e)
            pieces.append(Piece(doc.get("nom", "?"), f"[Téléchargement impossible : {e}]"))

    texte_dce, scans, utilisees = assembler(pieces)
    contenu: list[dict] = []
    for scan in scans:
        contenu.append({
            "type": "document",
            "source": {"type": "base64", "media_type": "application/pdf", "data": base64.b64encode(scan.pdf_brut).decode()},
            "title": scan.nom,
        })
    texte = (
        "# Avis de marché\n" + _entete(ao)
        + "\n## Texte de l'avis\n" + (ao.get("description") or "(non disponible)")
        + "\n\n# Référentiel de prix ADR\n" + _referentiel(store)
        + "\n\n# Pièces du DCE déposées par l'équipe\n"
        + (texte_dce or "(aucune pièce déposée : analyse limitée à l'avis)")
        + "\n\nProduis la fiche de décision avec l'outil `fiche_ao`."
    )
    contenu.append({"type": "text", "text": texte})
    return contenu, ["Avis de marché"] + utilisees


OUTIL = {"name": "fiche_ao", "description": "Enregistre la fiche de décision de l'appel d'offres.", "input_schema": SCHEMA}
CONSIGNE_OUTIL = "\n\nTu DOIS répondre en appelant l'outil `fiche_ao`, une seule fois, sans autre texte."


LISTES = ("points_majeurs", "pieces_a_fournir", "exigences_techniques", "risques", "questions_acheteur",
          "informations_manquantes", "lots", "criteres_attribution")
ENUMS = {
    "recommandation": ("go", "a_etudier", "no_go"),
    "categorie_recommandee": ("directe", "groupement", "sous_traitance", "non_pertinente"),
}


def _json_ou(v):
    """Certains modèles renvoient les sous-objets sous forme de chaîne JSON."""
    if isinstance(v, str):
        t = v.strip()
        if t[:1] in "[{":
            try:
                return json.loads(t)
            except json.JSONDecodeError:
                pass
    return v


def _nombre(v):
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return v
    if isinstance(v, str):
        chiffres = "".join(c for c in v.replace(",", ".") if c.isdigit() or c == ".")
        try:
            return float(chiffres) if chiffres else None
        except ValueError:
            return None
    return None


def normaliser_fiche(fiche) -> dict:
    """Remet la fiche au format attendu par le site, quelle que soit la forme renvoyée."""
    fiche = _json_ou(fiche)
    if not isinstance(fiche, dict):
        raise RuntimeError("Fiche de synthèse illisible")
    f = {k: _json_ou(v) for k, v in fiche.items()}

    for cle in LISTES:
        v = f.get(cle)
        if v is None:
            continue
        if isinstance(v, str):
            v = [l.strip(" -•\t") for l in v.splitlines() if l.strip(" -•\t")]
        elif isinstance(v, dict):
            v = [v]
        elif not isinstance(v, list):
            v = [str(v)]
        f[cle] = [_json_ou(x) for x in v]
    f["lots"] = [x if isinstance(x, dict) else {"intitule": str(x), "pertinence_adr": "moyenne"} for x in f.get("lots") or []]
    f["criteres_attribution"] = [x if isinstance(x, dict) else {"critere": str(x)} for x in f.get("criteres_attribution") or []]
    for cle in LISTES:
        if cle not in ("lots", "criteres_attribution") and f.get(cle) is not None:
            f[cle] = [x if isinstance(x, str) else json.dumps(x, ensure_ascii=False) for x in f[cle]]

    visite = f.get("visite")
    if isinstance(visite, str):
        f["visite"] = {"obligatoire": None, "details": visite}
    elif visite is not None and not isinstance(visite, dict):
        f["visite"] = {"obligatoire": bool(visite), "details": ""}
    if isinstance(f.get("visite"), dict) and isinstance(f["visite"].get("obligatoire"), str):
        f["visite"]["obligatoire"] = {"true": True, "oui": True, "false": False, "non": False}.get(f["visite"]["obligatoire"].strip().lower())

    e = f.get("estimation_cout")
    if isinstance(e, str) or e is None:
        e = {"base": e or "", "perimetre": "", "fiabilite": "faible"}
    elif not isinstance(e, dict):
        e = {"base": str(e), "perimetre": "", "fiabilite": "faible"}
    for k in ("montant_min_ht", "montant_max_ht"):
        e[k] = _nombre(e.get(k))
    if isinstance(e.get("hypotheses"), str):
        e["hypotheses"] = [l.strip(" -•") for l in e["hypotheses"].splitlines() if l.strip(" -•")]
    if e.get("fiabilite") not in ("faible", "moyenne", "bonne"):
        e["fiabilite"] = "faible"
    f["estimation_cout"] = e

    for cle, valeurs in ENUMS.items():
        v = str(f.get(cle) or "").strip().lower().replace("-", "_").replace(" ", "_")
        f[cle] = v if v in valeurs else ("a_etudier" if cle == "recommandation" else None)
    for cle in ("resume", "justification", "planning", "date_limite"):
        if f.get(cle) is not None and not isinstance(f[cle], str):
            f[cle] = json.dumps(f[cle], ensure_ascii=False)
    return f


def _extraire_fiche(rep) -> dict | None:
    """Fiche transmise via l'outil, ou à défaut un objet JSON dans le texte."""
    for bloc in rep.content:
        if getattr(bloc, "type", None) == "tool_use" and bloc.name == "fiche_ao":
            return bloc.input
    texte = "".join(getattr(b, "text", "") for b in rep.content if getattr(b, "type", None) == "text")
    debut, fin = texte.find("{"), texte.rfind("}")
    if debut >= 0 and fin > debut:
        try:
            fiche = json.loads(texte[debut : fin + 1])
            if isinstance(fiche, dict) and "resume" in fiche:
                return fiche
        except json.JSONDecodeError:
            pass
    return None


def appeler_claude(contenu: list[dict], client=None) -> dict:
    """Appel compatible avec les modèles qui refusent un outil imposé (tool_choice « tool »)."""
    if client is None:
        import anthropic
        client = anthropic.Anthropic()
    messages = [{"role": "user", "content": contenu}]
    params = dict(model=MODELE, max_tokens=16000, system=SYSTEME + CONSIGNE_OUTIL, tools=[OUTIL])
    rep = client.messages.create(messages=messages, **params)
    fiche = _extraire_fiche(rep)
    if fiche is not None:
        return fiche
    # Le modèle a répondu sans utiliser l'outil : une relance explicite
    messages += [
        {"role": "assistant", "content": [b.model_dump() if hasattr(b, "model_dump") else b for b in rep.content]},
        {"role": "user", "content": "Enregistre maintenant la fiche en appelant l'outil `fiche_ao`."},
    ]
    if any(getattr(b, "type", None) == "tool_use" for b in rep.content):
        raise RuntimeError("Claude a appelé un outil inattendu")
    fiche = _extraire_fiche(client.messages.create(messages=messages, **params))
    if fiche is None:
        raise RuntimeError("Réponse de Claude sans fiche structurée (après relance)")
    return fiche


def traiter(ao: dict, store: Store) -> bool:
    id_ = ao["id"]
    store.maj(id_, {"synthese_statut": "en_cours"})
    try:
        contenu, utilisees = preparer(ao, store)
        fiche = normaliser_fiche(appeler_claude(contenu))
        maj = {
            "synthese": fiche,
            "synthese_statut": "prete",
            "synthese_le": maintenant(),
            "synthese_modele": MODELE,
            "synthese_pieces": utilisees,
            "synthese_erreur": None,
            "categorie_ia": fiche.get("categorie_recommandee"),
        }
        # La visite obligatoire est remontée au niveau de la fiche pour l'affichage
        visite = (fiche.get("visite") or {}).get("obligatoire")
        if visite is not None:
            maj["visite_obligatoire"] = visite
        store.maj(id_, maj)
        store.evenement(id_, "synthese", {"pieces": utilisees, "recommandation": fiche.get("recommandation")})
        log.info("Synthèse prête : %s", id_)
        return True
    except Exception as e:
        log.exception("Synthèse %s", id_)
        store.maj(id_, {"synthese_statut": "erreur", "synthese_erreur": str(e)[:500]})
        return False


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s : %(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("--local", action="store_true")
    p.add_argument("--max", type=int, default=10, help="nombre maximal de synthèses par exécution")
    a = p.parse_args()
    charger_profil()  # vérifie que la configuration est lisible
    if not os.environ.get("ANTHROPIC_API_KEY", "").strip():
        # On ne touche pas à la file d'attente : les AO restent « en attente »
        raise SystemExit(
            "Secret GitHub ANTHROPIC_API_KEY manquant ou vide : à créer dans Settings → Secrets and variables → "
            "Actions → Repository secrets. Les synthèses restent en attente."
        )
    store = ouvrir(local=a.local)
    file_attente = store.synthese_demandee(a.max)
    log.info("%d synthèse(s) à produire", len(file_attente))
    echecs = sum(not traiter(ao, store) for ao in file_attente)
    if echecs:
        # Exécution marquée en échec dans GitHub (et dans « Bilan ») pour que l'erreur se voie
        raise SystemExit(f"{echecs} synthèse(s) sur {len(file_attente)} en échec : voir les erreurs ci-dessus.")


if __name__ == "__main__":
    main()
