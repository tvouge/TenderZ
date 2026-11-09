"""Collecte : interroge les sources, note, dédoublonne et met à jour la mémoire.

Usage :
  python -m veille.collect                 # production (Supabase)
  python -m veille.collect --local         # essai, mémoire dans memoire_locale.json
  python -m veille.collect --recap         # force l'envoi du récapitulatif
"""
from __future__ import annotations

import argparse
import logging
import time
from datetime import datetime, timedelta

from . import digest
from .models import Avis
from .scoring import NOTATION_VERSION, noter
from .sources import boamp, ted
from .store import Store, ouvrir
from .util import PARIS, charger_profil, maintenant, parser_date

log = logging.getLogger("veille.collect")

JOURS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]


def _idf_boamp(a: Avis, profil: dict) -> bool:
    return any(d in profil["departements_idf"] for d in a.departements)


def collecter(profil: dict) -> tuple[list[tuple[Avis, bool]], list[str], dict]:
    """Retourne [(avis, est_idf)], erreurs, statistiques par source."""
    depuis = boamp.depuis_jours(profil["fenetre_jours"])
    resultats: list[tuple[Avis, bool]] = []
    erreurs: list[str] = []
    stats: dict[str, int] = {}

    if profil["boamp"]["actif"]:
        lignes, err = boamp.lister(profil, depuis)
        erreurs += err
        stats["boamp_lus"] = len(lignes)
        # Pré-notation sur l'objet seul, puis détail des seuls candidats
        legers = [boamp.vers_avis(l) for l in lignes]
        seuil_detail = max(0, profil["seuils"]["conserver"] - 15)
        candidats = [
            a for a in legers
            if noter(a, profil, _idf_boamp(a, profil)).score >= seuil_detail
            or a.nature in ("rectificatif", "annulation")
        ]
        details = boamp.detailler([a.ref for a in candidats])
        par_id = {str(l.get("idweb") or l.get("id")): l for l in lignes}
        for a in candidats:
            complet = boamp.vers_avis(par_id[a.ref], details.get(a.ref))
            resultats.append((complet, _idf_boamp(complet, profil)))
        stats["boamp_detailles"] = len(candidats)

    if profil["ted"]["actif"]:
        notices, err = ted.lister(profil, depuis)
        erreurs += err
        stats["ted_lus"] = len(notices)
        for n in notices:
            a = ted.vers_avis(n)
            resultats.append((a, ted.est_idf(a, profil["ted"]["nuts_idf_prefixe"])))

    return resultats, erreurs, stats


def appliquer_modification(store: Store, original: dict, avis: Avis) -> None:
    """Rectificatif / annulation BOAMP rattaché à un avis déjà suivi."""
    maj: dict = {"modifie": True, "nouveaute": "modifie", "maj_veille_le": maintenant()}
    details = {"avis": avis.ref, "url": avis.url, "nature": avis.nature}
    if avis.nature == "annulation":
        maj["avis_annule"] = True
        type_ = "annulation"
    else:
        type_ = "rectificatif"
        if avis.date_limite and avis.date_limite != parser_date(original.get("date_limite")):
            details["ancienne_date_limite"] = original.get("date_limite")
            details["nouvelle_date_limite"] = avis.date_limite
            maj["date_limite"] = avis.date_limite
    maj["rectificatifs"] = sorted(set(original.get("rectificatifs", [])) | {avis.ref})
    store.maj(original["id"], maj)
    store.evenement(original["id"], type_, details)


def integrer(store: Store, avis: Avis, idf: bool, profil: dict) -> str:
    """Retourne : 'nouveau', 'modifie', 'inchange', 'ignore', 'rattache'."""
    # 1. Rectificatif / annulation d'un avis déjà suivi
    if avis.nature in ("rectificatif", "annulation") and avis.avis_lies:
        originaux = [o for o in (store.get(f"{avis.source}-{ref}") for ref in avis.avis_lies) if o]
        a_traiter = [o for o in originaux if avis.ref not in (o.get("rectificatifs") or [])]
        for original in a_traiter:
            appliquer_modification(store, original, avis)
        if a_traiter:
            return "rattache"
        if originaux:
            return "inchange"  # déjà rattaché lors d'une collecte précédente
        # Avis d'origine inconnu (publié avant la fenêtre) : suivi comme un AO à part

    note = noter(avis, profil, idf)
    if note.score < profil["seuils"]["conserver"] and not note.a_surveiller:
        return "ignore"

    existant = store.get(avis.id) or store.par_cle_doublon(avis.cle_doublon)
    doc = avis.vers_document()
    doc.update(
        score=note.score,
        score_detail=note.detail,
        idf=idf,
        titre_court=note.titre_court,
        travaux=note.travaux,
        a_faire=note.a_faire,
        montant_estime=note.montant,
        notation_v=NOTATION_VERSION,
        maj_veille_le=maintenant(),
    )

    if existant:
        id_ = existant["id"]
        if existant["source"] != avis.source:
            # Même consultation publiée sur une autre source (BOAMP + TED)
            autres = set(existant.get("autres_sources", [])) | {avis.url}
            store.maj(id_, {"autres_sources": sorted(autres), "vu_le": maintenant()})
            return "inchange"
        if existant.get("version") == avis.version:
            store.maj(id_, {"vu_le": maintenant()})
            return "inchange"
        def comparable(k, v):
            return parser_date(v) if k == "date_limite" else v
        changements = {
            k: {"avant": existant.get(k), "apres": doc.get(k)}
            for k in ("titre", "date_limite", "cpv")
            if comparable(k, existant.get(k)) != comparable(k, doc.get(k))
        }
        # On ne réécrit pas la catégorie si l'équipe l'a corrigée
        doc["categorie"] = note.categorie
        if existant.get("categorie_corrigee"):
            doc.pop("categorie", None)
        doc.update(modifie=True, nouveaute="modifie")
        doc.pop("id", None)
        store.maj(id_, doc)
        store.evenement(id_, "modification", changements)
        return "modifie"

    seuil_auto = profil["seuils"].get("synthese_auto") or 0
    auto = seuil_auto and note.score >= seuil_auto and avis.nature == "appel_offre"
    doc.update(
        categorie=note.categorie,
        statut="a_qualifier",
        nouveaute="nouveau",
        modifie=False,
        detecte_le=maintenant(),
        vu_le=maintenant(),
        responsable=None,
        synthese_statut="demandee" if auto else "aucune",
        documents=[],
        rectificatifs=[],
    )
    store.creer(avis.id, doc)
    store.evenement(avis.id, "detection", {"score": note.score, "categorie": note.categorie})
    return "nouveau"


def avis_depuis_document(d: dict) -> Avis:
    """Reconstitue un avis à partir d'une ligne déjà enregistrée (pour le renotage)."""
    return Avis(
        source=d.get("source") or "?", ref=str(d.get("ref") or d["id"]), titre=d.get("titre") or "", url=d.get("url") or "",
        acheteur=d.get("acheteur") or "", nature=d.get("nature") or "", type_marche=d.get("type_marche") or [],
        departements=d.get("departements") or [], lieu=d.get("lieu") or "", nuts=d.get("nuts") or [],
        cpv=d.get("cpv") or [], descripteurs=d.get("descripteurs") or [], description=d.get("description") or "",
        montant_estime=d.get("montant_estime"),
    )


def renoter(store: Store, profil: dict, tout: bool = False, budget_s: float = 420) -> int:
    """Applique les règles de notation actuelles aux AO enregistrés avec d'anciennes règles.

    Les dossiers déjà pris en charge ou décidés gardent leur score et leur catégorie
    (on ne les fait pas disparaître des listes) ; ils reçoivent seulement le titre court,
    les étiquettes de travaux et le budget.
    Le travail s'arrête au bout de `budget_s` secondes : le reste est repris à la collecte suivante,
    ce qui évite de dépasser le délai maximal du job quand beaucoup d'AO changent de règles.
    """
    try:
        lignes = store.a_renoter(NOTATION_VERSION, tout)
    except Exception as e:  # colonne notation_v absente (migration non lancée) ou API injoignable
        log.warning("Renotage ignoré : %s", e)
        return 0
    n = 0
    fin = time.monotonic() + budget_s
    for d in lignes:
        if time.monotonic() > fin:
            log.info("Renotage interrompu après %d AO (budget de temps) : suite à la prochaine collecte", n)
            break
        try:
            note = noter(avis_depuis_document(d), profil, bool(d.get("idf")))
            maj = dict(titre_court=note.titre_court, travaux=note.travaux, a_faire=note.a_faire, montant_estime=note.montant,
                       notation_v=NOTATION_VERSION)
            if d.get("statut") == "a_qualifier":
                maj.update(score=note.score, score_detail=note.detail)
                if not d.get("categorie_corrigee"):
                    maj["categorie"] = note.categorie
            store.maj(d["id"], maj)
            n += 1
        except Exception:
            log.exception("Renotage %s", d.get("id"))
    if n:
        log.info("%d AO renotés (règles v%d)", n, NOTATION_VERSION)
    return n


def executer(local: bool = False, forcer_recap: bool = False, sans_mail: bool = False, renoter_tout: bool = False) -> dict:
    profil = charger_profil()
    store = ouvrir(local=local)
    debut = maintenant()
    resultats, erreurs, stats = collecter(profil)

    compteur = {"nouveau": 0, "modifie": 0, "inchange": 0, "ignore": 0, "rattache": 0}
    for avis, idf in resultats:
        try:
            compteur[integrer(store, avis, idf, profil)] += 1
        except Exception as e:
            erreurs.append(f"Intégration {avis.id} : {e}")
            log.exception("Intégration %s", avis.id)

    renotes = renoter(store, profil, tout=renoter_tout)

    compte_rendu = {
        "debut": debut,
        "fin": maintenant(),
        "fenetre_jours": profil["fenetre_jours"],
        "sources": stats,
        "resultats": {**compteur, "renote": renotes},
        "erreurs": erreurs,
    }
    store.enregistrer_collecte(debut.astimezone(PARIS).strftime("%Y-%m-%d_%H%M"), compte_rendu)
    log.info("Collecte terminée : %s — erreurs : %d", compteur, len(erreurs))

    jour = JOURS[datetime.now(PARIS).weekday()]
    if not sans_mail and (forcer_recap or jour == profil["recap"]["jour"]):
        envoyer_recap(store, compte_rendu, profil)
    return compte_rendu


def envoyer_recap(store: Store, compte_rendu: dict, profil: dict) -> bool:
    """Envoie le récapitulatif ; un échec du mail est signalé mais ne fait pas échouer la collecte
    (elle est déjà enregistrée)."""
    depuis = maintenant() - timedelta(days=7)
    liste = digest.destinataires(store.parametre("recap_destinataires"))
    try:
        return digest.envoyer(store.detectes_depuis(depuis), compte_rendu, profil, liste)
    except digest.ErreurMail as e:
        log.error("Récapitulatif non envoyé : %s", e)
        print(f"::warning title=Récapitulatif non envoyé::{e}", flush=True)
        compte_rendu["recap_erreur"] = str(e)
        return False


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s : %(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("--local", action="store_true", help="mémoire locale (essai)")
    p.add_argument("--recap", action="store_true", help="forcer l'envoi du récapitulatif")
    p.add_argument("--sans-mail", action="store_true")
    p.add_argument("--renoter", action="store_true", help="renoter tous les AO avec les règles actuelles")
    p.add_argument("--test-mail", action="store_true", help="envoyer un mail de test et s'arrêter")
    a = p.parse_args()
    if a.test_mail:
        store = ouvrir(local=a.local)
        try:
            dest = digest.envoyer_test(digest.destinataires(store.parametre("recap_destinataires")))
        except digest.ErreurMail as e:
            raise SystemExit(f"Mail de test NON envoyé : {e}")
        log.info("Mail de test envoyé à %s", ", ".join(dest))
        return
    cr = executer(local=a.local, forcer_recap=a.recap, sans_mail=a.sans_mail, renoter_tout=a.renoter)
    if cr["erreurs"] and not any(cr["sources"].values()):
        raise SystemExit("Aucune source n'a répondu : voir les erreurs ci-dessus.")


if __name__ == "__main__":
    main()
