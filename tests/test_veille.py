import io
import json
import zipfile
from datetime import date, datetime, timedelta

import pytest

from veille import collect, digest
from veille.documents import Piece, assembler, extraire
from veille.models import Avis
from veille.scoring import noter, urgence
from veille.sources import boamp, ted
from veille.store import FichierStore
from veille.util import PARIS, charger_profil, contient, normaliser, parser_date

PROFIL = charger_profil()
DANS = lambda j: (datetime.now(PARIS) + timedelta(days=j)).strftime("%Y-%m-%dT12:00:00+01:00")


def ligne_boamp(**k):
    base = {
        "idweb": "26-100001",
        "objet": "Travaux d'agencement et menuiserie intérieure du musée Carnavalet",
        "nomacheteur": "Ville de Paris",
        "code_departement": ["75"],
        "dateparution": date.today().isoformat(),
        "datelimitereponse": DANS(30),
        "nature": "APPEL_OFFRE",
        "type_marche": ["TRAVAUX"],
        "descripteur_libelle": ["Menuiserie"],
        "url_avis": "https://www.boamp.fr/pages/avis/?q=idweb:26-100001",
    }
    base.update(k)
    return base


DONNEES = {
    "FNSimple": {
        "initial": {
            "natureMarche": {"description": "Lot 4 : menuiseries intérieures bois, habillages muraux et banque d'accueil"},
            "CPV": {"objetPrincipal": {"classPrincipale": "45421000"}},
            "renseignementsRecours": {"description": "Tribunal administratif de Paris, assurance nettoyage"},
        }
    }
}


# --- utilitaires -----------------------------------------------------------

def test_normaliser_et_contient():
    assert normaliser("Second Œuvre  ÉLÉMENTS") == "second oeuvre elements"
    assert contient(normaliser("Création d'un bar et d'un stand"), "bar")
    assert not contient(normaliser("Barrières de sécurité"), "bar")
    assert not contient(normaliser("standard téléphonique"), "stand")


def test_parser_date_formats():
    assert parser_date("2026-11-20").hour == 23
    assert parser_date("20261120").day == 20
    assert parser_date("2026-11-20+01:00").tzinfo is not None
    assert parser_date("2026-11-20T12:00:00Z").hour == 12
    assert parser_date("pas une date") is None
    assert parser_date(["2026-12-01", "2026-11-20"]).day == 20


# --- BOAMP -----------------------------------------------------------------

def test_boamp_conversion_et_cpv():
    a = boamp.vers_avis(ligne_boamp(annonce_lie="26-099999"), json.loads(json.dumps(DONNEES)))
    assert a.id == "boamp-26-100001"
    assert a.nature == "appel_offre"
    assert a.cpv == ["45421000"]
    assert "banque d'accueil" in a.description
    assert "Tribunal" not in a.description  # bloc recours exclu
    assert a.avis_lies == ["26-099999"]


def test_boamp_requetes():
    r = boamp.requetes(PROFIL, date(2026, 10, 15))
    assert "dateparution >= date'2026-10-15'" in r[0][1]
    assert 'code_departement="93"' in r[0][1]
    assert '"scénographie"' in r[1][1]


# --- TED -------------------------------------------------------------------

def test_ted_conversion():
    n = {
        "publication-number": "712345-2026",
        "notice-title": {"fra": "France – Mobilier – Agencement de la BnF", "eng": "France – Furniture"},
        "buyer-name": {"fra": ["Bibliothèque nationale de France"]},
        "publication-date": "2026-11-02+01:00",
        "deadline-receipt-tender-date-lot": ["2026-12-10+01:00"],
        "classification-cpv": ["39150000", "45421000"],
        "place-of-performance": ["FRA", "FR101"],
        "notice-type": "cn-standard",
        "links": {"html": {"FRA": "https://ted.europa.eu/fr/notice/-/detail/712345-2026"}},
    }
    a = ted.vers_avis(n)
    assert a.titre.startswith("France – Mobilier")
    assert a.acheteur == "Bibliothèque nationale de France"
    assert a.nature == "appel_offre"
    assert ted.est_idf(a)
    assert a.date_limite.day == 10
    assert "classification-cpv IN (45420000" in ted.construire_requete(PROFIL, date(2026, 10, 15))


# --- notation ---------------------------------------------------------------

def test_notation_directe_idf():
    a = boamp.vers_avis(ligne_boamp(), DONNEES)
    n = noter(a, PROFIL, idf=True)
    assert n.score >= 70
    assert n.categorie == "directe"


def test_notation_groupement():
    a = boamp.vers_avis(ligne_boamp(objet="Réhabilitation tous corps d'état du collège Jean Jaurès"), DONNEES)
    n = noter(a, PROFIL, idf=True)
    assert n.categorie == "groupement"


def test_notation_hors_metier():
    a = boamp.vers_avis(ligne_boamp(objet="Marché de nettoyage des locaux administratifs", descripteur_libelle=["Nettoyage"]))
    n = noter(a, PROFIL, idf=True)
    assert n.score < PROFIL["seuils"]["conserver"]
    # Le mot « assurance » dans le bloc recours ne doit pas pénaliser un bon avis
    a2 = boamp.vers_avis(ligne_boamp(), DONNEES)
    assert not any("assurance" in d for d in noter(a2, PROFIL, True).detail)


def test_attribution_sous_traitance():
    a = boamp.vers_avis(ligne_boamp(nature="ATTRIBUTION", titulaire="Bouygues Bâtiment"), DONNEES)
    assert noter(a, PROFIL, True).categorie == "sous_traitance"


def test_urgence():
    for jours, attendu in ((5, "critique"), (15, "urgente"), (40, "normale"), (-2, "expiree")):
        a = Avis(source="x", ref="1", titre="t", url="u", date_limite=parser_date(DANS(jours)))
        assert urgence(a, PROFIL)[0] == attendu


# --- mémoire et dédoublonnage -----------------------------------------------

@pytest.fixture
def store(tmp_path):
    return FichierStore(tmp_path / "m.json")


def test_cycle_complet(store):
    a = boamp.vers_avis(ligne_boamp(), DONNEES)
    assert collect.integrer(store, a, True, PROFIL) == "nouveau"
    doc = store.get(a.id)
    assert doc["statut"] == "a_qualifier" and doc["synthese_statut"] == "demandee"

    # Même avis relu à la collecte suivante : pas de doublon
    assert collect.integrer(store, boamp.vers_avis(ligne_boamp(), DONNEES), True, PROFIL) == "inchange"

    # Même consultation remontée par TED : rattachée, pas recréée
    t = Avis(source="ted", ref="1-2026", titre=a.titre, url="https://ted/1", acheteur=a.acheteur, date_limite=a.date_limite, cpv=["45421000"])
    assert collect.integrer(store, t, True, PROFIL) == "inchange"
    assert store.get(a.id)["autres_sources"] == ["https://ted/1"]
    assert store.get(t.id) is None

    # Rectificatif BOAMP avec report de date
    rect = boamp.vers_avis(ligne_boamp(idweb="26-100050", nature="RECTIFICATIF", annonce_lie="26-100001", datelimitereponse=DANS(45)))
    assert collect.integrer(store, rect, True, PROFIL) == "rattache"
    doc = store.get(a.id)
    assert doc["modifie"] and doc["rectificatifs"] == ["26-100050"]
    assert doc["date_limite"] == rect.date_limite
    types = [e["type"] for e in store.data["evenements"][a.id]]
    assert types == ["detection", "rectificatif"]

    # Rectificatif déjà traité : sans effet
    assert collect.integrer(store, rect, True, PROFIL) == "inchange"


def test_modification_preserve_categorie_corrigee(store):
    a = boamp.vers_avis(ligne_boamp(), DONNEES)
    collect.integrer(store, a, True, PROFIL)
    store.maj(a.id, {"categorie": "groupement", "categorie_corrigee": True})
    b = boamp.vers_avis(ligne_boamp(datelimitereponse=DANS(50)), DONNEES)
    assert collect.integrer(store, b, True, PROFIL) == "modifie"
    assert store.get(a.id)["categorie"] == "groupement"


# --- récapitulatif ----------------------------------------------------------

def test_recap(store):
    a = boamp.vers_avis(ligne_boamp(datelimitereponse=DANS(6)), DONNEES)
    collect.integrer(store, a, True, PROFIL)
    sujet, corps = digest.construire(list(store.data["aos"].values()), {"sources": {"boamp_lus": 120}, "resultats": {"nouveau": 1}, "erreurs": ["TED : HTTP 400"]}, PROFIL)
    assert "Tenders" in sujet
    assert "Réponse directe ADR (1)" in corps and "critique" in corps and "TED : HTTP 400" in corps
    _, vide = digest.construire([], {"sources": {}, "resultats": {}, "erreurs": []}, PROFIL)
    assert "Aucune opportunité pertinente" in vide


# --- documents --------------------------------------------------------------

def test_extraction_zip_docx_et_priorite():
    import docx

    d = docx.Document()
    d.add_paragraph("CCTP lot 4 : menuiseries intérieures en chêne, classement feu M1")
    buf_docx = io.BytesIO()
    d.save(buf_docx)
    z = io.BytesIO()
    with zipfile.ZipFile(z, "w") as f:
        f.writestr("DCE/04_CCTP_lot4.docx", buf_docx.getvalue())
        f.writestr("DCE/01_RC.txt", "Règlement de consultation : visite obligatoire le 12/11")
        f.writestr("DCE/plan.dwg", b"\x00\x01")
    pieces = extraire("DCE.zip", z.getvalue())
    assert len(pieces) == 2
    texte, scans, utilisees = assembler(pieces)
    assert utilisees[0].endswith("01_RC.txt")
    assert "chêne" in texte and not scans


def test_budget_assemblage():
    texte, _, utilisees = assembler([Piece("RC.txt", "a" * 100), Piece("CCTP.txt", "b" * 100)], budget=150)
    assert len(texte) < 300 and utilisees[1].endswith("(tronquée)")


# --- synthèse (Claude simulé) -------------------------------------------------

def test_synthese_bout_en_bout(store, tmp_path, monkeypatch):
    from veille import synthesis

    a = boamp.vers_avis(ligne_boamp(), DONNEES)
    collect.integrer(store, a, True, PROFIL)
    dce = tmp_path / "RC.txt"
    dce.write_text("Règlement : visite obligatoire le 10/11. Critères : technique 60 %, prix 40 %.", encoding="utf-8")
    store.maj(a.id, {"documents": [{"nom": "RC.txt", "chemin": str(dce)}]})

    vu = {}
    def faux_claude(contenu):
        vu["texte"] = contenu[-1]["text"]
        return {"resume": "ok", "points_majeurs": ["x"], "estimation_cout": {"perimetre": "lot 4", "base": "avis", "fiabilite": "faible"},
                "categorie_recommandee": "directe", "recommandation": "go", "justification": "ok", "visite": {"obligatoire": True}}
    monkeypatch.setattr(synthesis, "appeler_claude", faux_claude)

    for ao in store.synthese_demandee():
        synthesis.traiter(ao, store)
    doc = store.get(a.id)
    assert doc["synthese_statut"] == "prete" and doc["visite_obligatoire"] is True
    assert "technique 60 %" in vu["texte"] and "musée Carnavalet" in vu["texte"]
    assert doc["synthese_pieces"] == ["Avis de marché", "RC.txt"]

    # Une erreur de Claude est enregistrée sans bloquer la file
    monkeypatch.setattr(synthesis, "appeler_claude", lambda c: (_ for _ in ()).throw(RuntimeError("quota")))
    store.maj(a.id, {"synthese_statut": "demandee"})
    synthesis.traiter(store.get(a.id), store)
    assert store.get(a.id)["synthese_statut"] == "erreur" and "quota" in store.get(a.id)["synthese_erreur"]


def test_destinataires_recap(monkeypatch):
    monkeypatch.setenv("RECAP_DESTINATAIRES", "secours@adr.fr")
    assert digest.destinataires(["lea@adr.fr", " thibaud@adr.fr ", ""]) == ["lea@adr.fr", "thibaud@adr.fr"]
    assert digest.destinataires(None) == ["secours@adr.fr"]
    assert digest.destinataires([]) == ["secours@adr.fr"]


def test_referentiel_prix_du_site(store):
    from veille import synthesis

    assert synthesis._referentiel(store) == "(non renseigné par l'équipe)"  # fichier YAML vide par défaut
    store.data["parametres"] = {"referentiel_prix": {
        "taux_horaire_atelier": 55, "marge_cible": None, "remarques": "Laque sous-traitée",
        "ouvrages": [{"nom": "Banque d'accueil", "unite": "ml", "prix_min": 1800, "prix_max": 2600, "note": "chêne"},
                     {"nom": "Claustra", "unite": "m²", "prix_min": None, "prix_max": None, "note": ""}]}}
    texte = synthesis._referentiel(store)
    assert "Banque d'accueil" in texte and "1800" in texte and "taux_horaire_atelier: 55" in texte
    assert "Claustra" not in texte and "marge_cible" not in texte and "Laque" in texte


def test_synthese_sans_cle_ne_marque_pas_en_echec(store, monkeypatch):
    from veille import synthesis
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(synthesis, "ouvrir", lambda local=False: store)
    a = boamp.vers_avis(ligne_boamp(), DONNEES)
    collect.integrer(store, a, True, PROFIL)
    monkeypatch.setattr("sys.argv", ["synthesis"])
    with pytest.raises(SystemExit, match="ANTHROPIC_API_KEY"):
        synthesis.main()
    assert store.get(a.id)["synthese_statut"] == "demandee"


def test_variables_github_vides(monkeypatch):
    """GitHub transmet "" pour une variable non définie : les valeurs par défaut doivent s'appliquer."""
    import importlib
    from veille import synthesis
    monkeypatch.setenv("CLAUDE_MODEL", "")
    assert importlib.reload(synthesis).MODELE == "claude-sonnet-5-5"
    monkeypatch.setenv("CLAUDE_MODEL", "claude-opus-5-5")
    assert importlib.reload(synthesis).MODELE == "claude-opus-5-5"

    envoyes = {}
    class FauxSMTP:
        def __init__(self, hote, port, timeout): envoyes.update(hote=hote, port=port)
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def login(self, u, p): pass
        def sendmail(self, de, a, m): envoyes.update(de=de, a=a)
    monkeypatch.setattr(digest.smtplib, "SMTP_SSL", FauxSMTP)
    for v in ("SMTP_HOST", "SMTP_PORT", "RECAP_EXPEDITEUR"):
        monkeypatch.setenv(v, "")
    monkeypatch.setenv("SMTP_USER", "veille.adr@gmail.com"); monkeypatch.setenv("SMTP_PASSWORD", "x")
    assert digest.envoyer([], {"sources": {}, "resultats": {}, "erreurs": []}, PROFIL, ["lea@adr.fr"])
    assert envoyes == {"hote": "smtp.gmail.com", "port": 465, "de": "veille.adr@gmail.com", "a": ["lea@adr.fr"]}


# --- appel Claude : sans outil imposé -----------------------------------------------

class _Bloc:
    def __init__(self, **k): self.__dict__.update(k)
    def model_dump(self): return dict(self.__dict__)


class _FauxClient:
    def __init__(self, reponses): self.reponses, self.appels = list(reponses), []
    @property
    def messages(self): return self
    def create(self, **k):
        self.appels.append(k)
        return _Bloc(content=self.reponses.pop(0))


FICHE = {"resume": "ok", "points_majeurs": [], "estimation_cout": {"perimetre": "", "base": "", "fiabilite": "faible"},
         "categorie_recommandee": "directe", "recommandation": "go", "justification": "ok"}


def test_claude_outil_direct():
    from veille import synthesis
    c = _FauxClient([[_Bloc(type="thinking", thinking="…"), _Bloc(type="tool_use", name="fiche_ao", input=FICHE, id="t1")]])
    assert synthesis.appeler_claude([{"type": "text", "text": "x"}], client=c) == FICHE
    assert "tool_choice" not in c.appels[0]  # refusé par certains modèles


def test_claude_relance_puis_outil():
    from veille import synthesis
    c = _FauxClient([[_Bloc(type="text", text="Voici mon analyse…")],
                     [_Bloc(type="tool_use", name="fiche_ao", input=FICHE, id="t2")]])
    assert synthesis.appeler_claude([{"type": "text", "text": "x"}], client=c) == FICHE
    assert len(c.appels) == 2 and c.appels[1]["messages"][-1]["role"] == "user"


def test_claude_json_dans_le_texte_et_echec():
    import json as j
    from veille import synthesis
    c = _FauxClient([[_Bloc(type="text", text="```json\n" + j.dumps(FICHE) + "\n```")]])
    assert synthesis.appeler_claude([], client=c) == FICHE
    c = _FauxClient([[_Bloc(type="text", text="rien")], [_Bloc(type="text", text="toujours rien")]])
    with pytest.raises(RuntimeError, match="après relance"):
        synthesis.appeler_claude([], client=c)


def test_normaliser_fiche_formes_variees():
    import json as j
    from veille.synthesis import normaliser_fiche
    brute = {
        "resume": "Agencement de l'accueil",
        "points_majeurs": "- Lot 4 séparé\n- Visite obligatoire",
        "visite": j.dumps({"obligatoire": "oui", "details": "10/11 à 9 h"}),
        "estimation_cout": j.dumps({"montant_min_ht": "145 000 €", "montant_max_ht": 185000, "perimetre": "lot 4",
                                    "base": "DPGF", "hypotheses": "9 ml\n220 m²", "fiabilite": "moyenne"}),
        "lots": j.dumps([{"numero": "4", "intitule": "Menuiserie", "pertinence_adr": "forte"}, "Lot 6 électricité"]),
        "criteres_attribution": ["Prix 40 %"],
        "risques": {"texte": "pénalités"},
        "recommandation": "Go",
        "categorie_recommandee": "réponse directe",
        "justification": "ok",
    }
    f = normaliser_fiche(j.dumps(brute))
    assert f["points_majeurs"] == ["Lot 4 séparé", "Visite obligatoire"]
    assert f["visite"] == {"obligatoire": True, "details": "10/11 à 9 h"}
    e = f["estimation_cout"]
    assert e["montant_min_ht"] == 145000 and e["montant_max_ht"] == 185000 and e["hypotheses"] == ["9 ml", "220 m²"]
    assert f["lots"][0]["intitule"] == "Menuiserie" and f["lots"][1]["intitule"] == "Lot 6 électricité"
    assert f["criteres_attribution"] == [{"critere": "Prix 40 %"}]
    assert f["risques"] == ['{"texte": "pénalités"}']
    assert f["recommandation"] == "go" and f["categorie_recommandee"] is None
    assert normaliser_fiche({"resume": "x", "visite": "Non précisée", "estimation_cout": "Non chiffrable"})["visite"]["details"] == "Non précisée"
    with pytest.raises(RuntimeError):
        normaliser_fiche("pas du json")


# --- notation v2 : région, budget, périmètre, étiquettes ---------------------

from veille.analyse import extraire_montant_texte, titre_court  # noqa: E402
from veille.scoring import NOTATION_VERSION  # noqa: E402


def avis_test(titre, description="", cpv=("45421000",), **k):
    return Avis(source="boamp", ref="t1", titre=titre, url="u", cpv=list(cpv), description=description,
                departements=["75"], **k)


def test_budget_dans_la_cible_et_hors_cible():
    base = "Agencement et menuiserie intérieure d'un siège"
    dans = noter(avis_test(base, montant_estime=250_000), PROFIL, True)
    petit = noter(avis_test(base, montant_estime=40_000), PROFIL, True)
    gros = noter(avis_test(base, montant_estime=3_000_000), PROFIL, True)
    inconnu = noter(avis_test(base), PROFIL, True)
    assert dans.score > inconnu.score > petit.score > gros.score
    assert any("dans la cible" in d for d in dans.detail)
    assert any("non communiqué" in d for d in inconnu.detail)


def test_budget_lu_dans_le_texte_et_les_donnees_boamp():
    assert extraire_montant_texte("Le montant estimé du marché est de 250 000 euros HT") == 250_000
    assert extraire_montant_texte("budget prévisionnel : 1,2 millions d'euros") == 1_200_000
    assert extraire_montant_texte("aucun chiffre") is None
    assert boamp.extraire_montant({"A": {"VAL_ESTIMEE": "300000", "DEVISE": "EUR"}}) == 300_000


def test_region_hors_idf_penalisee_mais_possible():
    a = avis_test("Agencement et scénographie d'un musée")
    idf = noter(a, PROFIL, True)
    ailleurs = noter(a, PROFIL, False)
    assert ailleurs.score >= PROFIL["seuils"]["pertinent"]  # reste « à qualifier »
    assert any("déplacement envisageable" in d for d in ailleurs.detail)
    # Contenu moyen : la pénalité hors IDF s'applique
    moyen = avis_test("Fourniture de mobilier", cpv=())
    assert noter(moyen, PROFIL, True).score - noter(moyen, PROFIL, False).score == PROFIL["bonus_idf"] - PROFIL["notation"]["region"]["hors_idf"]


def test_marche_global_penalise_et_lot_metier_favorise():
    global_ = noter(avis_test("Réhabilitation tous corps d'état d'un collège"), PROFIL, True)
    lot = noter(avis_test("Lot 4 : menuiseries intérieures bois d'un collège"), PROFIL, True)
    assert lot.score > global_.score
    assert any("marché global" in d for d in global_.detail)
    assert any("Lot métier" in d for d in lot.detail)


def test_plus_de_la_moitie_hors_metier_est_ecartee():
    texte = "Rénovation : plâtrerie, peinture, plomberie, électricité et pose de quelques portes"
    n = noter(avis_test("Réhabilitation d'un bâtiment", texte, cpv=()), PROFIL, True)
    assert any("hors métier" in d and "moitié" in d for d in n.detail)
    assert n.score < PROFIL["seuils"]["pertinent"]
    propre = noter(avis_test("Fourniture de mobilier sur mesure et de portes", "menuiserie intérieure, habillages muraux"), PROFIL, True)
    assert propre.score >= PROFIL["seuils"]["synthese_auto"]


def test_marche_alloti_avec_lot_metier_nest_pas_ecarte():
    texte = "Lot 1 : plâtrerie, peinture\nLot 2 : plomberie, électricité\nLot 3 : menuiserie intérieure et portes"
    n = noter(avis_test("Réhabilitation d'une école", texte, cpv=()), PROFIL, True)
    assert not any("moitié" in d for d in n.detail)


def test_etiquettes_de_travaux_et_titre_court():
    a = avis_test("MARCHE DE TRAVAUX POUR L'AGENCEMENT DU MUSEE", "banque d'accueil, métallerie, vitrerie, peinture", cpv=())
    n = noter(a, PROFIL, True)
    types = {t["n"]: t["t"] for t in n.travaux}
    assert types["Agencement"] == "adr" and types["Métallerie"] == "ss" and types["Peinture et sols"] == "hors"
    assert titre_court("Accord-cadre pour la fourniture de mobilier").startswith("La fourniture")
    assert len(titre_court("x " * 200)) <= 90
    assert titre_court("Lot 2 : Menuiseries intérieures").startswith("Lot 2 : menuiseries")


def test_renotage_des_anciennes_regles(store):
    a = boamp.vers_avis(ligne_boamp(), DONNEES)
    collect.integrer(store, a, True, PROFIL)
    store.maj(a.id, {"notation_v": 1, "score": 3, "travaux": [], "titre_court": None})
    pris = boamp.vers_avis(ligne_boamp(idweb="26-100002", objet="Agencement d'une médiathèque"), DONNEES)
    collect.integrer(store, pris, True, PROFIL)
    store.maj(pris.id, {"notation_v": 1, "score": 3, "statut": "pris_en_charge"})
    assert collect.renoter(store, PROFIL) == 2
    assert store.get(a.id)["score"] > 3 and store.get(a.id)["notation_v"] == NOTATION_VERSION
    assert store.get(a.id)["titre_court"] and store.get(a.id)["travaux"]
    assert store.get(pris.id)["score"] == 3          # dossier déjà pris : score conservé
    assert store.get(pris.id)["travaux"]             # mais étiquettes ajoutées
    assert collect.renoter(store, PROFIL) == 0       # rien à refaire


def test_apercu_a_faire_extrait_de_l_avis():
    from veille.analyse import a_faire
    texte = ("Lot 4 : menuiseries intérieures bois, habillages muraux et banque d'accueil. "
             "Fourniture de la serrurerie des issues de secours.\nPlâtrerie et peinture des circulations. "
             "Le candidat remet son offre sur la plateforme.")
    pts = a_faire(avis_test("Rénovation d'un collège", texte, cpv=()), PROFIL)
    assert pts[0].startswith("Lot 4") and "banque d'accueil" in pts[0]
    assert any(p.startswith("À confier à un sous-traitant") and "serrurerie" in p for p in pts)
    assert not any("plateforme" in p or "Plâtrerie" in p for p in pts)
    assert a_faire(avis_test("x", ""), PROFIL) == []
    assert len(a_faire(avis_test("x", "Menuiserie bois. " * 40 + "Portes palières."), PROFIL, maxi=3)) <= 3


def test_menuiserie_exterieure_hors_activite():
    ext = noter(avis_test("Remplacement des menuiseries extérieures bois d'une école", cpv=("45421132",)), PROFIL, True)
    assert ext.score < PROFIL["seuils"]["pertinent"]
    assert not any(t["n"] == "Menuiserie intérieure" for t in ext.travaux)
    assert ext.a_surveiller and {"n": "Menuiserie extérieure", "t": "veille"} in ext.travaux
    assert not any(d.startswith("CPV 45421132") for d in ext.detail)
    interieur = noter(avis_test("Menuiseries intérieures bois d'une école", cpv=("45421000",)), PROFIL, True)
    assert interieur.score >= PROFIL["seuils"]["synthese_auto"]
    # Un mot générique « menuiserie » dans « menuiseries extérieures » ne compte pas, mais le reste de l'avis si
    mixte = noter(avis_test("Menuiseries extérieures et agencement de la banque d'accueil", cpv=()), PROFIL, True)
    assert any("agencement" in d for d in mixte.detail) and not any(d.startswith("« menuiserie") and "(+" in d for d in mixte.detail)
    from veille.analyse import a_faire
    pts = a_faire(avis_test("x", "Remplacement des menuiseries extérieures bois. Pose de portes intérieures en chêne."), PROFIL)
    assert pts == ["Pose de portes intérieures en chêne."]


def test_menuiserie_exterieure_conservee_a_part(store):
    # Score nul, mais gardée en base pour l'onglet « À surveiller »
    ext = boamp.vers_avis(ligne_boamp(idweb="26-200001", objet="Remplacement des menuiseries extérieures bois du groupe scolaire"))
    assert collect.integrer(store, ext, True, PROFIL) == "nouveau"
    doc = store.get(ext.id)
    assert doc["score"] < PROFIL["seuils"]["pertinent"] and doc["categorie"] == "non_pertinente"
    assert any(t["t"] == "veille" for t in doc["travaux"]) and doc["synthese_statut"] == "aucune"
    # Un dossier qui contient du métier ADR n'est pas « à surveiller », même avec des menuiseries extérieures
    mixte = noter(avis_test("Menuiseries extérieures et agencement de l'accueil"), PROFIL, True)
    assert not mixte.a_surveiller
    # Hors sujet (nettoyage de fenêtres) : toujours écarté
    sale = boamp.vers_avis(ligne_boamp(idweb="26-200002", objet="Nettoyage des fenêtres des bâtiments communaux"))
    assert collect.integrer(store, sale, True, PROFIL) == "ignore"


# --- envoi du mail : robustesse -----------------------------------------------------

def _regler_smtp(monkeypatch, mdp="abcd efgh\u00a0ijkl mnop"):
    for v in ("SMTP_HOST", "SMTP_PORT", "RECAP_EXPEDITEUR"):
        monkeypatch.setenv(v, "")
    monkeypatch.setenv("SMTP_USER", " veille.adr@gmail.com\n"); monkeypatch.setenv("SMTP_PASSWORD", mdp)


def test_mail_espaces_retires_et_repli_sur_587(monkeypatch):
    import smtplib
    vus = []
    class Coupe:   # le port 465 coupe la connexion à l'authentification, comme Gmail le 9 novembre
        def __init__(self, hote, port, timeout): vus.append(port)
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def login(self, u, p): raise smtplib.SMTPServerDisconnected("Connection unexpectedly closed")
    class Starttls:
        def __init__(self, hote, port, timeout): vus.append(port)
        def ehlo(self): pass
        def starttls(self): vus.append("tls")
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def login(self, u, p): vus.append((u, p))
        def sendmail(self, de, a, m): vus.append("envoye")
    monkeypatch.setattr(digest.smtplib, "SMTP_SSL", Coupe)
    monkeypatch.setattr(digest.smtplib, "SMTP", Starttls)
    _regler_smtp(monkeypatch)
    assert digest.envoyer_test(["lea@adr.fr"]) == ["lea@adr.fr"]
    assert vus == [465, 587, "tls", ("veille.adr@gmail.com", "abcdefghijklmnop"), "envoye"]


def test_mail_erreur_explicite_et_collecte_non_bloquee(monkeypatch, store):
    import smtplib
    class Refuse:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def login(self, u, p): raise smtplib.SMTPAuthenticationError(535, b"refused")
    monkeypatch.setattr(digest.smtplib, "SMTP_SSL", Refuse)
    _regler_smtp(monkeypatch)
    with pytest.raises(digest.ErreurMail, match="mot de passe d'application"):
        digest.envoyer_test(["lea@adr.fr"])
    store.parametre = lambda cle: ["lea@adr.fr"]
    cr = {"sources": {}, "resultats": {}, "erreurs": []}
    assert collect.envoyer_recap(store, cr, PROFIL) is False   # pas d'exception : la collecte reste verte
    assert "mot de passe d'application" in cr["recap_erreur"]
    monkeypatch.delenv("SMTP_USER")
    with pytest.raises(digest.ErreurMail, match="SMTP_USER"):
        digest.envoyer_test(["lea@adr.fr"])


def test_renotage_limite_dans_le_temps(store):
    for i in range(3):
        a = boamp.vers_avis(ligne_boamp(idweb=f"26-30000{i}", objet=f"Agencement d'une salle {i}"), DONNEES)
        collect.integrer(store, a, True, PROFIL)
        store.maj(a.id, {"notation_v": 1})
    assert collect.renoter(store, PROFIL, budget_s=-1) == 0     # budget épuisé : rien fait, reprise plus tard
    assert collect.renoter(store, PROFIL) == 3
