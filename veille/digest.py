"""Récapitulatif hebdomadaire par mail (SMTP, par exemple Gmail avec mot de passe d'application)."""
from __future__ import annotations

import html
import logging
import os
import smtplib
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from .scoring import ORDRE_CATEGORIE, ORDRE_URGENCE
from .util import PARIS, jours_restants, parser_date

log = logging.getLogger(__name__)

LIBELLES = {
    "directe": "Réponse directe ADR",
    "groupement": "Réponse en groupement",
    "sous_traitance": "Sous-traitance probable",
    "non_pertinente": "Peu pertinent",
}
COULEURS = {"critique": "#b42318", "urgente": "#b54708", "normale": "#344054", "inconnue": "#667085", "expiree": "#98a2b3"}


def _urgence(ao: dict, profil: dict) -> tuple[str, int | None]:
    j = jours_restants(parser_date(ao.get("date_limite")))
    if j is None:
        return "inconnue", None
    if j < 0:
        return "expiree", j
    if j < profil["urgence"]["critique"]:
        return "critique", j
    if j < profil["urgence"]["urgente"]:
        return "urgente", j
    return "normale", j


def _fmt_date(v) -> str:
    d = parser_date(v)
    return d.astimezone(PARIS).strftime("%d/%m/%Y %H:%M") if d else "—"


def construire(aos: list[dict], compte_rendu: dict, profil: dict) -> tuple[str, str]:
    date_txt = datetime.now(PARIS).strftime("%d/%m/%Y")
    sujet = f"Tenders — L'Atelier du Renard — semaine du {date_txt}"
    site = profil["recap"]["url_site"].rstrip("/")

    retenus = [a for a in aos if a.get("categorie") != "non_pertinente" and a.get("statut") != "abandonne"]
    for a in retenus:
        a["_urg"], a["_j"] = _urgence(a, profil)
    retenus = [a for a in retenus if a["_urg"] != "expiree"]
    retenus.sort(key=lambda a: (ORDRE_CATEGORIE.get(a.get("categorie"), 9), ORDRE_URGENCE[a["_urg"]], -a.get("score", 0)))

    blocs = []
    for cat in ("directe", "groupement", "sous_traitance"):
        lignes = [a for a in retenus if a.get("categorie") == cat]
        if not lignes:
            continue
        rangs = "".join(_ligne(a, site) for a in lignes)
        blocs.append(
            f"<h3 style='margin:24px 0 8px'>{LIBELLES[cat]} ({len(lignes)})</h3>"
            "<table cellpadding='6' style='border-collapse:collapse;width:100%;font-size:13px'>"
            "<tr style='background:#f2f4f7;text-align:left'><th>Échéance</th><th>Score</th><th>Objet</th>"
            "<th>Acheteur</th><th>Lieu</th><th>Statut</th></tr>"
            f"{rangs}</table>"
        )

    s = compte_rendu.get("sources", {})
    r = compte_rendu.get("resultats", {})
    erreurs = compte_rendu.get("erreurs", [])
    sources_txt = (
        f"BOAMP : {s.get('boamp_lus', 0)} avis lus ({s.get('boamp_detailles', 0)} analysés en détail) · "
        f"TED : {s.get('ted_lus', 0)} avis lus"
    )
    obstacles = (
        "<ul>" + "".join(f"<li>{html.escape(e)}</li>" for e in erreurs) + "</ul>" if erreurs else "<p>Aucun.</p>"
    )
    corps_ao = "".join(blocs) or "<p><b>Aucune opportunité pertinente nouvelle ou modifiée cette semaine.</b></p>"

    corps = f"""<div style="font-family:Arial,sans-serif;color:#101828;max-width:900px">
<h2 style="margin-bottom:4px">Tenders — appels d'offres du {date_txt}</h2>
<p style="color:#475467;margin-top:0">Nouveaux et modifiés sur les 7 derniers jours ·
{r.get('nouveau', 0)} nouveaux, {r.get('modifie', 0) + r.get('rattache', 0)} modifiés lors de la dernière collecte.
<a href="{site}">Ouvrir le tableau de suivi</a></p>
{corps_ao}
<h3 style="margin-top:28px">Sources vérifiées</h3><p>{sources_txt}</p>
<p style="color:#475467">Les portails sur compte (PLACE, Maximilien, RATP, SNCF, ADP…) ne sont pas interrogés
automatiquement : les avis repérés par l'équipe s'ajoutent à la main dans le site.</p>
<h3>Obstacles rencontrés</h3>{obstacles}
</div>"""
    return sujet, corps


def _ligne(a: dict, site: str) -> str:
    couleur = COULEURS[a["_urg"]]
    j = a["_j"]
    ech = f"{_fmt_date(a.get('date_limite'))}<br><b style='color:{couleur}'>" + (
        f"J-{j} · {a['_urg']}" if j is not None else "date inconnue"
    ) + "</b>"
    badge = " <span style='background:#fef0c7;padding:1px 6px;border-radius:8px'>modifié</span>" if a.get("nouveaute") == "modifie" else ""
    statut = (a.get("statut") or "").replace("_", " ")
    if a.get("responsable"):
        statut += f" ({html.escape(a['responsable'].get('nom', ''))})"
    return (
        "<tr style='border-top:1px solid #eaecf0;vertical-align:top'>"
        f"<td>{ech}</td><td><b>{a.get('score', 0)}</b></td>"
        f"<td><a href='{site}/#/ao/{a['id']}'>{html.escape(a.get('titre', ''))}</a>{badge}"
        f"<br><a href='{a.get('url')}' style='color:#667085;font-size:12px'>avis officiel ({a.get('source')})</a></td>"
        f"<td>{html.escape(a.get('acheteur', ''))}</td><td>{html.escape(a.get('lieu', ''))}</td>"
        f"<td>{html.escape(statut)}</td></tr>"
    )


def destinataires(reglage) -> list[str]:
    """Liste réglée dans le site (page Équipe), à défaut la variable GitHub RECAP_DESTINATAIRES."""
    if isinstance(reglage, list) and any(str(d).strip() for d in reglage):
        return [str(d).strip() for d in reglage if str(d).strip()]
    return [d.strip() for d in os.environ.get("RECAP_DESTINATAIRES", "").split(",") if d.strip()]


def envoyer(aos: list[dict], compte_rendu: dict, profil: dict, liste: list[str] | None = None) -> bool:
    destinataires = liste if liste is not None else globals()["destinataires"](None)
    utilisateur = os.environ.get("SMTP_USER")
    mot_de_passe = os.environ.get("SMTP_PASSWORD")
    if not (destinataires and utilisateur and mot_de_passe):
        log.warning("Récap non envoyé : SMTP_USER, SMTP_PASSWORD ou destinataires manquants")
        return False
    sujet, corps = construire(aos, compte_rendu, profil)
    msg = MIMEMultipart("alternative")
    msg["Subject"] = sujet
    msg["From"] = os.environ.get("RECAP_EXPEDITEUR") or utilisateur
    msg["To"] = ", ".join(destinataires)
    msg.attach(MIMEText(corps, "html", "utf-8"))
    hote = os.environ.get("SMTP_HOST") or "smtp.gmail.com"
    port = int(os.environ.get("SMTP_PORT") or "465")
    with smtplib.SMTP_SSL(hote, port, timeout=60) as s:
        s.login(utilisateur, mot_de_passe)
        s.sendmail(msg["From"], destinataires, msg.as_string())
    log.info("Récap envoyé à %s", ", ".join(destinataires))
    return True
