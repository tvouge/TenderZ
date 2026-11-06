// Tenders — L'Atelier du Renard
// Application d'équipe : suivi, prise en charge et décisions sur les appels d'offres.
import { createClient } from "https://cdn.jsdelivr.net/npm/@supabase/supabase-js@2/+esm";
import { SUPABASE_URL, SUPABASE_ANON_KEY } from "./config.js";

const sb = createClient(SUPABASE_URL, SUPABASE_ANON_KEY, {
  auth: { flowType: "pkce", detectSessionInUrl: true, persistSession: true },
});
const BUCKET = "dce";
// Colonnes chargées pour la liste (le texte intégral de l'avis reste côté serveur)
const COLONNES_BASE = "id,source,ref,titre,url,acheteur,nature,type_marche,procedure,lieu,cpv,date_publication,date_limite,titulaire,portail," +
  "score,score_detail,categorie,categorie_corrigee,categorie_ia,statut,responsable,motif_abandon,commentaire_abandon,notes,montant_depose," +
  "documents,synthese,synthese_statut,synthese_le,synthese_pieces,synthese_erreur,visite_obligatoire,nouveaute,modifie,avis_annule," +
  "rectificatifs,autres_sources,detecte_le,maj_veille_le,maj_par,maj_le";
// Colonnes ajoutées par supabase/003_travaux.sql (la liste fonctionne aussi tant qu'elles n'existent pas)
const COLONNES_NOUVELLES = ",titre_court,travaux,montant_estime";
const maintenantISO = () => new Date().toISOString();

// --- Référentiels -----------------------------------------------------------

const STATUTS = {
  a_qualifier: "À qualifier",
  pris_en_charge: "Pris en charge",
  en_redaction: "Réponse en cours",
  depose: "Offre déposée",
  gagne: "Gagné",
  perdu: "Perdu",
  abandonne: "Abandonné",
};
const ONGLETS = [
  ["a_qualifier", "À qualifier", ["a_qualifier"]],
  ["en_cours", "En cours", ["pris_en_charge", "en_redaction"]],
  ["depose", "Déposés", ["depose"]],
  ["clos", "Gagnés / perdus", ["gagne", "perdu"]],
  ["abandonne", "Abandonnés", ["abandonne"]],
  ["manques", "Faute de ressources", []], // règle propre : voir estManque()
  ["tous", "Tous", Object.keys(STATUTS)],
];
const CATEGORIES = {
  directe: "Réponse directe ADR",
  groupement: "Groupement",
  sous_traitance: "Sous-traitance",
  non_pertinente: "Peu pertinent",
};
const MOTIFS = {
  delai_insuffisant: "Délai insuffisant pour répondre",
  hors_competence: "Hors compétence / hors métier",
  montant_faible: "Montant trop faible",
  montant_eleve: "Montant trop élevé / capacité financière",
  charge_atelier: "Charge de l'atelier (pas de disponibilité)",
  concurrence: "Concurrence / titulaire sortant installé",
  exigences_administratives: "Exigences (références, qualifications, assurances)",
  visite_impossible: "Visite obligatoire impossible",
  localisation: "Localisation trop éloignée",
  marge_insuffisante: "Marge insuffisante / prix tirés",
  doublon: "Doublon d'un autre AO",
  autre: "Autre",
};
const RECOS = { go: "Go", a_etudier: "À étudier", no_go: "No go" };
const ORDRE_CAT = { directe: 0, groupement: 1, sous_traitance: 2, non_pertinente: 3 };
const ORDRE_URG = { critique: 0, urgente: 1, normale: 2, inconnue: 3, expiree: 4 };
const SEUIL_CRITIQUE = 10, SEUIL_URGENT = 21;
// Seuils de score : à garder identiques à config/profil_adr.yaml (rubrique « seuils »)
const SEUIL_SYNTHESE = 65; // synthèse IA de l'avis lancée automatiquement
const SEUIL_PERTINENT = 40; // en dessous : « peu pertinent »
// Un AO non commencé à moins de 7 jours de l'échéance n'est plus traitable : il passe dans « Faute de ressources »
const DELAI_MIN_REPONSE = 7;
// Budget visé : à garder identique à config/profil_adr.yaml (rubrique « notation »)
const BUDGET_IDEAL = [100000, 500000], BUDGET_MAX = 1000000;

// --- État -----------------------------------------------------------------

const etat = {
  utilisateur: null, // { email, nom, role }
  aos: new Map(),
  membres: new Map(),
  aosCharges: false,
  desabonnements: [],
  filtres: lireFiltres(),
};

function lireFiltres() {
  const defaut = { onglet: "a_qualifier", categorie: "", urgence: "", texte: "", miens: false, tri: "pertinence", scoreMin: String(SEUIL_PERTINENT), vue: "cartes" };
  try { return { ...defaut, ...JSON.parse(localStorage.getItem("veille-filtres") || "{}") }; }
  catch { return defaut; }
}
function sauverFiltres() {
  try { localStorage.setItem("veille-filtres", JSON.stringify(etat.filtres)); } catch { /* ignoré */ }
}

// --- Outils -----------------------------------------------------------------

const $ = (s, racine = document) => racine.querySelector(s);
const $$ = (s, racine = document) => [...racine.querySelectorAll(s)];
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const urlSure = (u) => (/^https?:\/\//i.test(u || "") ? esc(u) : "#");

function versDate(v) {
  if (!v) return null;
  if (v instanceof Date) return v;
  if (typeof v.toDate === "function") return v.toDate();
  if (typeof v === "object" && "seconds" in v) return new Date(v.seconds * 1000);
  const d = new Date(v);
  return isNaN(d) ? null : d;
}
const fmtDate = (v, heure = true) => {
  const d = versDate(v);
  if (!d) return "—";
  return d.toLocaleString("fr-FR", { timeZone: "Europe/Paris", day: "2-digit", month: "2-digit", year: "numeric", ...(heure ? { hour: "2-digit", minute: "2-digit" } : {}) });
};
const jourParis = (d) => d.toLocaleDateString("en-CA", { timeZone: "Europe/Paris" });
function joursRestants(v) {
  const d = versDate(v);
  if (!d) return null;
  return Math.round((new Date(jourParis(d)) - new Date(jourParis(new Date()))) / 86400000);
}
function urgence(ao) {
  const j = joursRestants(ao.date_limite);
  if (j === null) return ["inconnue", null];
  if (j < 0) return ["expiree", j];
  if (j < SEUIL_CRITIQUE) return ["critique", j];
  if (j < SEUIL_URGENT) return ["urgente", j];
  return ["normale", j];
}
function pastilleUrgence(ao) {
  const [u, j] = urgence(ao);
  const txt = { critique: `J-${j} · critique`, urgente: `J-${j} · urgent`, normale: `J-${j}`, expiree: "Clos", inconnue: "Date ?" }[u];
  return `<span class="pastille p-${u}">${txt}</span>`;
}
function pastilleScore(s) {
  if (s === null || s === undefined) return `<span class="score s-bas" title="Ajout manuel : pas de score">—</span>`;
  const [c, t] = s >= SEUIL_SYNTHESE ? ["s-haut", `Très pertinent (≥ ${SEUIL_SYNTHESE}) : synthèse IA lancée automatiquement`]
    : s >= SEUIL_PERTINENT ? ["s-moyen", `Pertinent (${SEUIL_PERTINENT}–${SEUIL_SYNTHESE - 1}) : synthèse IA à la demande`]
    : ["s-bas", `Peu pertinent (< ${SEUIL_PERTINENT})`];
  return `<span class="score ${c}" title="${t}">${esc(s)}</span>`;
}

const ETATS_SYNTHESE = {
  prete: ["p-normale", "✓ Synthèse prête"],
  demandee: ["p-urgente", "⏳ Synthèse en attente"],
  en_cours: ["p-urgente", "⏳ Synthèse en cours"],
  erreur: ["p-critique", "⚠ Synthèse en échec"],
};
// Synthèse rédigée sur l'avis seul (aucune pièce du DCE) : lancée automatiquement
// au-dessus du seuil de score, ou à la demande sans DCE déposé.
const surAvisSeul = (ao) => (ao.synthese_pieces || []).length <= 1;
const pastilleSynthese = (ao) => {
  const e = ETATS_SYNTHESE[ao.synthese_statut];
  if (!e) return "";
  const libelle = ao.synthese_statut === "prete" && surAvisSeul(ao) ? "✓ Synthèse prête (avis seul)" : e[1];
  const aide = ao.synthese_statut === "prete" && surAvisSeul(ao)
    ? ` title="Rédigée sur l'avis uniquement, sans pièces du DCE. Lancée automatiquement pour les AO dont le score est d'au moins ${SEUIL_SYNTHESE}."`
    : "";
  return `<span class="pastille ${e[0]}"${aide}>${libelle}</span>`;
};

function legendeEtiquettes() {
  return `<div class="legende">
    <span><span class="etiq e-adr">Portes</span> notre métier</span>
    <span><span class="etiq e-ss">Métallerie</span> à sous-traiter</span>
    <span><span class="etiq e-hors">Peinture</span> hors métier</span>
    <span>Survolez le score pour voir comment il est calculé</span>
  </div>`;
}

function legendeScores() {
  return `<div class="legende">
    <span><span class="score s-haut">${SEUIL_SYNTHESE}+</span> Très pertinent · synthèse IA automatique</span>
    <span><span class="score s-moyen">${SEUIL_PERTINENT}+</span> Pertinent · synthèse à la demande</span>
    <span><span class="score s-bas">&lt;${SEUIL_PERTINENT}</span> Peu pertinent</span>
  </div>`;
}
function badges(ao) {
  const b = [];
  const detecte = versDate(ao.detecte_le);
  if (ao.nouveaute === "nouveau" && detecte && Date.now() - detecte < 7 * 86400000) b.push(`<span class="pastille p-nouveau">Nouveau</span>`);
  if (ao.modifie) b.push(`<span class="pastille p-modifie">Modifié</span>`);
  if (ao.avis_annule) b.push(`<span class="pastille p-annule">Annulé</span>`);
  if (ao.visite_obligatoire) b.push(`<span class="pastille p-visite">Visite obligatoire</span>`);
  if (ETATS_SYNTHESE[ao.synthese_statut]) b.push(pastilleSynthese(ao));
  if (ao.source === "manuel") b.push(`<span class="pastille">Ajout manuel</span>`);
  return b.length ? `<div class="badges">${b.join("")}</div>` : "";
}
const nomCourt = (r) => (r ? esc(r.nom || r.email) : "");
const euros = (n) => (typeof n === "number" ? n.toLocaleString("fr-FR", { style: "currency", currency: "EUR", maximumFractionDigits: 0 }) : "?");

function toast(msg) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.add("visible");
  clearTimeout(toast.minuterie);
  toast.minuterie = setTimeout(() => t.classList.remove("visible"), 3000);
}
function erreur(e) {
  console.error(e);
  const msg = {
    "23514": "Règle non respectée : un abandon exige un motif et une explication, un dossier en cours un responsable.",
    "42501": "Action non autorisée pour votre compte.",
  }[e?.code] || `Erreur : ${e?.message || e}`;
  toast(msg);
}

// --- Écritures ----------------------------------------------------------------

async function majAO(id, champs, type, details = {}) {
  const { data, error } = await sb.from("aos").update(champs).eq("id", id).select(COLONNES);
  if (error) throw error;
  if (!data?.length) throw new Error("modification refusée");
  etat.aos.set(id, data[0]);
  const ev = await sb.from("evenements").insert({ ao_id: id, type, details, par: etat.utilisateur.email });
  if (ev.error) console.warn(ev.error);
  router();
  if (location.hash.startsWith("#/ao/")) afficherHistorique(id);
}
const moi = () => ({ email: etat.utilisateur.email, nom: etat.utilisateur.nom || etat.utilisateur.email });

// --- Authentification -----------------------------------------------------------

function vueConnexion(message = "") {
  $("#barre").hidden = true;
  $("#app").innerHTML = `
    <div class="connexion"><div class="carte">
      <svg viewBox="0 0 32 32"><path d="M4 6l7 6h10l7-6-3 14-9 8-9-8z"/></svg>
      <h1>Tenders</h1>
      <p class="aide">L'Atelier du Renard — accès réservé à l'équipe.</p>
      ${message ? `<p class="erreur">${esc(message)}</p>` : ""}
      <form id="form-connexion" style="text-align:left">
        <label for="c-mail">Adresse mail</label>
        <input type="email" id="c-mail" autocomplete="username" required>
        <label for="c-mdp">Mot de passe</label>
        <input type="password" id="c-mdp" autocomplete="current-password" required>
        <p class="erreur" id="err-connexion" hidden></p>
        <button class="btn" type="submit">Se connecter</button>
      </form>
      <p class="aide" style="margin-top:14px">Pas de compte ou mot de passe oublié ? Demandez à un administrateur.</p>
    </div></div>`;
  $("#form-connexion").onsubmit = async (e) => {
    e.preventDefault();
    const bouton = $("#form-connexion button");
    bouton.disabled = true;
    const { error } = await sb.auth.signInWithPassword({ email: $("#c-mail").value.trim().toLowerCase(), password: $("#c-mdp").value });
    bouton.disabled = false;
    if (error) {
      const err = $("#err-connexion");
      err.textContent = /invalid login/i.test(error.message) ? "Adresse ou mot de passe incorrect."
        : /rate limit|too many/i.test(error.message) ? "Trop de tentatives : patientez quelques minutes." : error.message;
      err.hidden = false;
    }
  };
}

function dialogueMotDePasse() {
  const d = $("#dialogue");
  d.innerHTML = `<form method="dialog" id="form-mdp">
    <h2>Changer mon mot de passe</h2>
    <p class="aide">${esc(etat.utilisateur.email)}</p>
    <label for="mdp1">Nouveau mot de passe (10 caractères minimum)</label>
    <input type="password" id="mdp1" autocomplete="new-password" required minlength="10">
    <label for="mdp2">Confirmation</label>
    <input type="password" id="mdp2" autocomplete="new-password" required minlength="10">
    <p class="erreur" id="err-mdp" hidden></p>
    <div class="pied-dialogue"><button type="button" class="btn btn-secondaire" id="annuler-mdp">Annuler</button><button class="btn">Enregistrer</button></div>
  </form>`;
  d.showModal();
  $("#annuler-mdp").onclick = () => d.close();
  $("#form-mdp").onsubmit = async (e) => {
    e.preventDefault();
    const a = $("#mdp1").value, b = $("#mdp2").value, err = $("#err-mdp");
    if (a.length < 10) { err.textContent = "10 caractères minimum."; err.hidden = false; return; }
    if (a !== b) { err.textContent = "Les deux saisies ne correspondent pas."; err.hidden = false; return; }
    const { error } = await sb.auth.updateUser({ password: a });
    if (error) { err.textContent = error.message; err.hidden = false; return; }
    d.close();
    toast("Mot de passe modifié.");
  };
}

let sessionCourante; // indéfini tant que la première session n'est pas connue
sb.auth.onAuthStateChange((_evt, session) => {
  const email = session?.user?.email?.toLowerCase() || null;
  if (email === sessionCourante) return; // simple rafraîchissement du jeton
  sessionCourante = email;
  setTimeout(() => surSession(email), 0);
});

async function surSession(email) {
  etat.desabonnements.forEach((f) => f());
  etat.desabonnements = [];
  etat.aos.clear();
  etat.aosCharges = false;
  if (location.search.includes("code=")) history.replaceState(null, "", location.pathname + "#/");
  if (!email) { etat.utilisateur = null; vueConnexion(); return; }
  const { data: fiche } = await sb.from("membres").select("*").eq("email", email).maybeSingle();
  if (!fiche) {
    await sb.auth.signOut();
    vueConnexion(`Le compte ${email} n'est pas autorisé. Demandez à un administrateur de vous ajouter dans « Équipe ».`);
    return;
  }
  etat.utilisateur = { email, nom: fiche.nom || email, role: fiche.role };
  demarrer();
}

$("#deconnexion").onclick = () => sb.auth.signOut();
$("#utilisateur").onclick = () => etat.utilisateur && dialogueMotDePasse();

// --- Données temps réel -----------------------------------------------------------

let avecNouvellesColonnes = true;

async function chargerAOs() {
  const depuis = new Date(Date.now() - 400 * 86400000).toISOString();
  const requete = (colonnes, de) => sb.from("aos").select(colonnes).gte("maj_veille_le", depuis)
    .order("maj_veille_le", { ascending: false }).range(de, de + 999);
  for (let de = 0; de < 6000; de += 1000) {
    let { data, error } = await requete(avecNouvellesColonnes ? COLONNES_BASE + COLONNES_NOUVELLES : COLONNES_BASE, de);
    if (error && avecNouvellesColonnes && error.code === "42703") { // colonne inconnue : migration 003 pas encore lancée
      avecNouvellesColonnes = false;
      ({ data, error } = await requete(COLONNES_BASE, de));
    }
    if (error) throw error;
    data.forEach((a) => etat.aos.set(a.id, a));
    if (data.length < 1000) break;
  }
  etat.aosCharges = true;
}

async function chargerMembres() {
  const { data, error } = await sb.from("membres").select("*");
  if (error) return erreur(error);
  etat.membres = new Map(data.map((m) => [m.email, m]));
}

async function demarrer() {
  $("#barre").hidden = false;
  $("#utilisateur").textContent = etat.utilisateur.nom;
  $("#nav-equipe").hidden = etat.utilisateur.role !== "admin";
  $("#nav-reglages").hidden = etat.utilisateur.role !== "admin";
  router();
  try { await Promise.all([chargerAOs(), chargerMembres()]); } catch (e) { erreur(e); }
  router();

  const canal = sb.channel("veille")
    .on("postgres_changes", { event: "*", schema: "public", table: "aos" }, (p) => {
      if (p.eventType === "DELETE") etat.aos.delete(p.old.id);
      else etat.aos.set(p.new.id, p.new);
      rafraichir();
    })
    .on("postgres_changes", { event: "*", schema: "public", table: "membres" }, async () => {
      await chargerMembres();
      if (location.hash.startsWith("#/equipe")) rafraichir();
    })
    .subscribe();
  etat.desabonnements.push(() => sb.removeChannel(canal));
}

// Ne pas redessiner une fiche pendant une saisie
function rafraichir() {
  const actif = document.activeElement;
  if (actif && ["TEXTAREA", "INPUT", "SELECT"].includes(actif.tagName) && actif.closest("#app") && !actif.closest(".filtres")) return;
  if ($("#dialogue").open) return;
  router();
}

// --- Routeur ---------------------------------------------------------------------

window.addEventListener("hashchange", () => etat.utilisateur && router());

let vueCourante = null;
function router() {
  const h = location.hash || "#/";
  const [, page, id] = h.split("/");
  const nav = page === "ao" || !page ? "liste" : page;
  $$("[data-nav]").forEach((a) => a.classList.toggle("actif", a.dataset.nav === nav));
  const cle = `${page}/${id || ""}`;
  const changement = cle !== vueCourante;
  vueCourante = cle;
  if (page === "ao" && id) return vueFiche(decodeURIComponent(id), changement);
  if (page === "bilan") return vueBilan();
  if (page === "equipe") return vueEquipe();
  if (page === "reglages") return changement && vueReglages();
  if (page === "ajouter") return changement && vueAjout();
  return vueListe();
}

// --- Liste ------------------------------------------------------------------------

// --- Délai, titres et étiquettes -------------------------------------------------------

function delaiTropCourt(a) {
  const j = joursRestants(a.date_limite);
  return j !== null && j < DELAI_MIN_REPONSE;
}
// Non commencé et trop tard (ou dépassé), ou abandonné faute de temps / de charge
const estManque = (a) => (a.statut === "a_qualifier" && delaiTropCourt(a))
  || (a.statut === "abandonne" && ["charge_atelier", "delai_insuffisant"].includes(a.motif_abandon));

const ACRONYMES = new Set(["ZAC", "PLV", "ERP", "DCE", "CPAM", "IUT", "IDF", "HLM", "TCE", "RATP", "SNCF", "CHU", "PMR", "ESAT", "CFA", "EHPAD", "CCAS", "OPH"]);
// Repli côté navigateur tant que le titre court n'est pas calculé par la collecte
function titreCourt(a) {
  if (a.titre_court) return a.titre_court;
  let t = String(a.titre || "").replace(/\s+/g, " ").trim().replace(/[.;:,]+$/, "");
  const lettres = t.replace(/[^A-Za-zÀ-ÿ]/g, "");
  if (lettres && (lettres.match(/[A-ZÀ-Þ]/g) || []).length / lettres.length > 0.6) {
    t = t.split(" ").map((m) => (ACRONYMES.has(m.replace(/[.,()]/g, "")) ? m : m.toLowerCase())).join(" ");
  }
  const avant = t;
  t = t.replace(/^march[ée]s?\s+(public\s+)?(de\s+|d')?(travaux|fournitures?|services?)\s*(pour|relatifs?\s+[àa]|:|-)?\s*/i, "")
    .replace(/^accords?[- ]cadres?\s*(pour|relatif\s+[àa]|:|-)?\s*/i, "")
    .replace(/^travaux\s+(de\s+|d['’]|pour\s+)/i, "").trim();
  if (t.length < 8) t = avant;
  t = t.charAt(0).toUpperCase() + t.slice(1);
  return t.length > 90 ? t.slice(0, 89).replace(/\s+\S*$/, "") + "…" : t;
}

const DEPARTEMENTS = { 75: "Paris", 77: "Seine-et-Marne", 78: "Yvelines", 91: "Essonne", 92: "Hauts-de-Seine", 93: "Seine-Saint-Denis", 94: "Val-de-Marne", 95: "Val-d'Oise",
  FR101: "Paris", FR102: "Seine-et-Marne", FR103: "Yvelines", FR104: "Essonne", FR105: "Hauts-de-Seine", FR106: "Seine-Saint-Denis", FR107: "Val-de-Marne", FR108: "Val-d'Oise" };
function libelleLieu(lieu) {
  const morceaux = String(lieu || "").split(",").map((x) => x.trim()).filter(Boolean);
  if (!morceaux.length) return "";
  return morceaux.map((c) => DEPARTEMENTS[c.toUpperCase()] || (/^\d{2,3}$/.test(c) ? `Dép. ${c}` : c)).join(", ");
}

const dateCourte = (v) => {
  const d = versDate(v);
  return d ? d.toLocaleDateString("fr-FR", { timeZone: "Europe/Paris", weekday: "short", day: "numeric", month: "short" }) : "";
};

const ICONES = {
  lieu: '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M8 14s4.5-4.2 4.5-7.5a4.5 4.5 0 1 0-9 0C3.5 9.8 8 14 8 14Z"/><circle cx="8" cy="6.5" r="1.6"/></svg>',
  acheteur: '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M2.5 14V3.5h7V14M9.5 7h4V14M5 6h2M5 9h2M5 12h2"/></svg>',
  budget: '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M11.5 4.2A4.3 4.3 0 1 0 11.5 11.8M3 6.8h6M3 9.2h6"/></svg>',
};
const SENS_ETIQUETTE = { adr: "Dans le métier de l'atelier", ss: "À confier à un sous-traitant", hors: "Hors métier" };

function budgetHtml(a) {
  if (!Object.hasOwn(a, "montant_estime")) return ""; // colonne pas encore créée
  const m = Number(a.montant_estime);
  if (!m) return `<span class="m m-budget inconnu" title="Aucun montant dans l'avis : à vérifier dans le DCE">${ICONES.budget}Budget non communiqué</span>`;
  const txt = m >= 1e6 ? `${(m / 1e6).toFixed(1).replace(".", ",")} M€` : `${Math.round(m / 1000).toLocaleString("fr-FR")} k€`;
  const cl = m >= BUDGET_IDEAL[0] && m <= BUDGET_IDEAL[1] ? "ideal" : m > BUDGET_IDEAL[1] && m <= BUDGET_MAX ? "haut" : "hors";
  const aide = { ideal: "Dans la cible (100 – 500 k€)", haut: "Au-dessus de la cible, encore jouable", hors: "En dehors de la fourchette visée" }[cl];
  return `<span class="m m-budget ${cl}" title="${aide}">${ICONES.budget}≈ ${txt} HT</span>`;
}

function carteAO(a, dansManques) {
  const [u, j] = urgence(a);
  const niveau = a.score === null || a.score === undefined ? "manuel" : a.score >= SEUIL_SYNTHESE ? "haut" : a.score >= SEUIL_PERTINENT ? "moyen" : "bas";
  const anneau = niveau === "manuel" ? `<div class="anneau manuel" title="Ajout manuel : pas de score"><b>+</b></div>`
    : `<div class="anneau" style="--p:${Math.max(0, Math.min(100, a.score))}" title="${esc((a.score_detail || []).join("\n") || "Score de pertinence")}"><b>${esc(a.score)}</b></div>`;
  const etiquettes = (a.travaux || []).map((t) => `<span class="etiq e-${esc(t.t)}" title="${esc(SENS_ETIQUETTE[t.t] || "")}">${esc(t.n)}</span>`).join("");
  const lieu = libelleLieu(a.lieu);
  const jours = u === "expiree" ? "Clos" : j === null ? "Date ?" : `J-${j}`;
  const statut = a.statut === "a_qualifier" && dansManques ? "Non traité" : (STATUTS[a.statut] || a.statut);
  return `<article class="ao n-${niveau} u-${u}" data-id="${esc(a.id)}" tabindex="0" role="link" aria-label="${esc(titreCourt(a))}">
    ${anneau}
    <div class="ao-corps">
      <div class="ao-haut"><span class="ao-cat c-${esc(a.categorie)}">${esc(CATEGORIES[a.categorie] || a.categorie)}</span>${badges(a)}</div>
      <div class="ao-titre" title="${esc(a.titre)}">${esc(titreCourt(a))}</div>
      ${etiquettes ? `<div class="ao-etiq">${etiquettes}</div>` : ""}
      <div class="ao-meta">
        ${lieu ? `<span class="m">${ICONES.lieu}${esc(lieu)}</span>` : ""}
        ${a.acheteur ? `<span class="m">${ICONES.acheteur}${esc(a.acheteur)}</span>` : ""}
        ${budgetHtml(a)}
      </div>
    </div>
    <div class="ao-echeance">
      <div class="jours">${jours}</div>
      <div class="date">${esc(dateCourte(a.date_limite))}</div>
      <div class="ao-statut">${esc(statut)}${a.responsable ? `<br>${nomCourt(a.responsable)}` : ""}</div>
    </div>
  </article>`;
}

function htmlCartes(liste, grouper, dansManques) {
  let html = "", catPrec = null;
  for (const a of liste) {
    if (grouper && a.categorie !== catPrec) {
      catPrec = a.categorie;
      html += `<div class="groupe-ao">${esc(CATEGORIES[a.categorie] || a.categorie)} · ${liste.filter((x) => x.categorie === a.categorie).length}</div>`;
    }
    html += carteAO(a, dansManques);
  }
  return `<div class="liste-ao">${html}</div>`;
}

function htmlTableau(liste, grouper) {
  let lignes = "", catPrec = null;
  for (const a of liste) {
    if (grouper && a.categorie !== catPrec) {
      catPrec = a.categorie;
      const n = liste.filter((x) => x.categorie === a.categorie).length;
      lignes += `<tr class="groupe-cat"><td colspan="6">${esc(CATEGORIES[a.categorie] || a.categorie)} · ${n}</td></tr>`;
    }
    lignes += `<tr data-id="${esc(a.id)}">
      <td>${pastilleUrgence(a)}<div class="sous">${fmtDate(a.date_limite, false)}</div></td>
      <td>${pastilleScore(a.score)}</td>
      <td><div class="titre-ao" title="${esc(a.titre)}">${esc(titreCourt(a))}</div><div class="sous">${esc(a.acheteur)}${grouper ? "" : ` · ${esc(CATEGORIES[a.categorie] || a.categorie)}`}</div>${badges(a)}</td>
      <td class="masquer-mobile">${esc(libelleLieu(a.lieu))}</td>
      <td>${esc(STATUTS[a.statut] || a.statut)}${a.responsable ? `<div class="sous">${nomCourt(a.responsable)}</div>` : ""}</td>
      <td class="masquer-mobile sous">${esc((a.source || "").toUpperCase())}<br>${fmtDate(a.date_publication, false)}</td>
    </tr>`;
  }
  return `<div class="tableau"><table>
      <thead><tr><th>Échéance</th><th>Score</th><th>Objet</th><th class="masquer-mobile">Lieu</th><th>Statut</th><th class="masquer-mobile">Source</th></tr></thead>
      <tbody>${lignes}</tbody></table></div>`;
}

function filtrer(aos, f) {
  const onglet = ONGLETS.find((o) => o[0] === f.onglet) || ONGLETS[0];
  const texte = f.texte.trim().toLowerCase();
  return aos.filter((a) => {
    if (f.onglet === "manques") { if (!estManque(a)) return false; }
    else if (!onglet[2].includes(a.statut)) return false;
    // Non commencé et trop proche de l'échéance : rangé dans « Faute de ressources »
    else if (f.onglet === "a_qualifier" && delaiTropCourt(a)) return false;
    if (f.categorie === "pertinentes" && a.categorie === "non_pertinente") return false;
    if (f.categorie && f.categorie !== "pertinentes" && a.categorie !== f.categorie) return false;
    if (f.urgence && urgence(a)[0] !== f.urgence) return false;
    // Les ajouts manuels (sans score) restent toujours visibles
    if (f.scoreMin && a.score !== null && a.score !== undefined && a.score < Number(f.scoreMin)) return false;
    if (f.miens && a.responsable?.email !== etat.utilisateur.email) return false;
    if (texte && !`${a.titre} ${a.acheteur} ${a.lieu} ${a.ref}`.toLowerCase().includes(texte)) return false;
    return true;
  });
}

function vueListe() {
  if (!etat.aosCharges) { $("#app").innerHTML = `<p class="chargement">Chargement des appels d'offres…</p>`; return; }
  const f = etat.filtres;
  const tous = [...etat.aos.values()];
  const parScore = (a, b) => (b.score ?? 101) - (a.score ?? 101); // ajouts manuels en tête
  const parEcheance = (a, b) => ORDRE_URG[urgence(a)[0]] - ORDRE_URG[urgence(b)[0]]
    || (versDate(a.date_limite) || Infinity) - (versDate(b.date_limite) || Infinity);
  const parCategorie = (a, b) => (ORDRE_CAT[a.categorie] ?? 9) - (ORDRE_CAT[b.categorie] ?? 9);
  const tri = { pertinence: (a, b) => parScore(a, b) || parEcheance(a, b),
                echeance: (a, b) => parEcheance(a, b) || parScore(a, b),
                categorie: (a, b) => parCategorie(a, b) || parEcheance(a, b) || parScore(a, b) }[f.tri] || ((a, b) => parScore(a, b));
  const liste = filtrer(tous, f).sort(tri);
  const grouper = f.tri === "categorie";

  const compte = (o) => filtrer(tous, { ...f, onglet: o[0], urgence: "", texte: "" }).length;
  const masques = filtrer(tous, { ...f, scoreMin: "" }).length - liste.length;
  const focus = document.activeElement?.id;

  const dansManques = f.onglet === "manques";
  const noteOnglet = dansManques ? `<p class="aide note-onglet">Dossiers que l'équipe n'a pas pu traiter faute de temps ou de ressources : non commencés à moins de ${DELAI_MIN_REPONSE} jours de l'échéance (ou échéance dépassée), et dossiers abandonnés pour délai insuffisant ou charge de l'atelier.</p>` : "";
  const corps = !liste.length ? `<p class="vide">Aucun appel d'offres ne correspond à ces filtres.</p>`
    : f.vue === "tableau" ? htmlTableau(liste, grouper) : htmlCartes(liste, grouper, dansManques);

  $("#app").innerHTML = `
    <div class="onglets">${ONGLETS.map((o) => `<button data-onglet="${o[0]}" class="${f.onglet === o[0] ? "actif" : ""}">${o[1]}<span class="nb">${compte(o)}</span></button>`).join("")}</div>
    <div class="filtres">
      <input type="search" id="f-texte" placeholder="Rechercher un objet, un acheteur, une référence…" value="${esc(f.texte)}">
      <select id="f-cat">
        <option value="">Tous les types de réponse</option>
        <option value="pertinentes" ${f.categorie === "pertinentes" ? "selected" : ""}>Sans les « peu pertinents »</option>
        ${Object.entries(CATEGORIES).map(([k, v]) => `<option value="${k}" ${f.categorie === k ? "selected" : ""}>${v}</option>`).join("")}
      </select>
      <select id="f-urg">
        <option value="">Toutes échéances</option>
        <option value="critique" ${f.urgence === "critique" ? "selected" : ""}>Critique (&lt; ${SEUIL_CRITIQUE} j)</option>
        <option value="urgente" ${f.urgence === "urgente" ? "selected" : ""}>Urgent (&lt; ${SEUIL_URGENT} j)</option>
        <option value="normale" ${f.urgence === "normale" ? "selected" : ""}>Plus de ${SEUIL_URGENT} j</option>
        <option value="expiree" ${f.urgence === "expiree" ? "selected" : ""}>Date dépassée</option>
      </select>
      <select id="f-score" title="Score minimum">
        <option value="${SEUIL_SYNTHESE}" ${f.scoreMin === String(SEUIL_SYNTHESE) ? "selected" : ""}>Score ≥ ${SEUIL_SYNTHESE}</option>
        <option value="${SEUIL_PERTINENT}" ${f.scoreMin === String(SEUIL_PERTINENT) ? "selected" : ""}>Score ≥ ${SEUIL_PERTINENT}</option>
        <option value="" ${!f.scoreMin ? "selected" : ""}>Tous les scores</option>
      </select>
      <select id="f-tri" title="Trier par">
        <option value="pertinence" ${f.tri === "pertinence" ? "selected" : ""}>Trier : pertinence</option>
        <option value="echeance" ${f.tri === "echeance" ? "selected" : ""}>Trier : échéance</option>
        <option value="categorie" ${f.tri === "categorie" ? "selected" : ""}>Trier : type de réponse</option>
      </select>
      <label class="case"><input type="checkbox" id="f-miens" ${f.miens ? "checked" : ""}> Mes dossiers</label>
      <div class="bascule" role="group" aria-label="Affichage">
        <button data-vue="cartes" class="${f.vue !== "tableau" ? "actif" : ""}">Cartes</button><button data-vue="tableau" class="${f.vue === "tableau" ? "actif" : ""}">Tableau</button>
      </div>
      ${etat.utilisateur.role === "admin" ? `<button class="btn btn-secondaire" id="lancer-collecte-liste" title="Interroger BOAMP et TED maintenant">↻ Lancer une collecte</button>` : ""}
    </div>
    ${legendeScores()}
    ${f.vue !== "tableau" ? legendeEtiquettes() : ""}
    ${masques > 0 ? `<p class="aide" style="margin:-6px 0 12px">${liste.length} AO affichés · ${masques} sous le score minimum masqués
      (<button class="btn-lien" id="voir-tous-scores">tout afficher</button>)</p>` : ""}
    ${noteOnglet}${corps}`;

  if (f.categorie === "") $("#f-cat").value = "";
  $$("[data-onglet]").forEach((b) => (b.onclick = () => { f.onglet = b.dataset.onglet; sauverFiltres(); vueListe(); }));
  $("#f-texte").oninput = (e) => { f.texte = e.target.value; sauverFiltres(); vueListe(); };
  $("#f-cat").onchange = (e) => { f.categorie = e.target.value; sauverFiltres(); vueListe(); };
  $("#f-urg").onchange = (e) => { f.urgence = e.target.value; sauverFiltres(); vueListe(); };
  $("#f-miens").onchange = (e) => { f.miens = e.target.checked; sauverFiltres(); vueListe(); };
  $("#f-score").onchange = (e) => { f.scoreMin = e.target.value; sauverFiltres(); vueListe(); };
  $("#f-tri").onchange = (e) => { f.tri = e.target.value; sauverFiltres(); vueListe(); };
  $("#voir-tous-scores") && ($("#voir-tous-scores").onclick = () => { f.scoreMin = ""; sauverFiltres(); vueListe(); });
  $("#lancer-collecte-liste") && ($("#lancer-collecte-liste").onclick = () => lancerWorkflow("collecte"));
  $$("[data-vue]").forEach((b) => (b.onclick = () => { f.vue = b.dataset.vue; sauverFiltres(); vueListe(); }));
  $$("tbody tr[data-id], .ao[data-id]").forEach((el) => {
    const ouvrir = () => (location.hash = `#/ao/${encodeURIComponent(el.dataset.id)}`);
    el.onclick = ouvrir;
    el.onkeydown = (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); ouvrir(); } };
  });
  if (focus === "f-texte") { const i = $("#f-texte"); i.focus(); i.setSelectionRange(i.value.length, i.value.length); }
}

// --- Fiche ----------------------------------------------------------------------------

let desabonnementHistorique = null;
let dernierHistorique = { id: null, html: "" };

function vueFiche(id, nouvelleVue) {
  const ao = etat.aos.get(id);
  if (!ao) {
    $("#app").innerHTML = etat.aosCharges ? `<a class="retour" href="#/">← Retour</a><p class="vide">Appel d'offres introuvable.</p>` : `<p class="chargement">Chargement…</p>`;
    return;
  }
  const [u, j] = urgence(ao);
  const delaiCourt = u === "critique" && ["a_qualifier", "pris_en_charge"].includes(ao.statut);
  const autres = (ao.autres_sources || []).map((s, i) => `<a href="${urlSure(s)}" target="_blank" rel="noopener">Autre publication ${i + 1} ↗</a>`).join("");

  $("#app").innerHTML = `
    <a class="retour" href="#/">← Tous les appels d'offres</a>
    <div class="fiche">
      <div>
        <div class="carte">
          <div class="badges" style="margin:0 0 8px">${pastilleUrgence(ao)} ${pastilleScore(ao.score)} <span class="pastille">${esc(CATEGORIES[ao.categorie] || ao.categorie)}</span></div>
          <h1>${esc(ao.titre)}</h1>
          <div class="sous">${esc(ao.acheteur)}</div>
          ${badges(ao)}
          ${delaiCourt ? `<p class="motif">Échéance dans ${j} jour(s) : vérifiez que le délai permet une réponse sérieuse (visite, échantillons, chiffrage).</p>` : ""}
          <div class="liens">
            ${ao.url ? `<a href="${urlSure(ao.url)}" target="_blank" rel="noopener"><b>Avis officiel (${esc((ao.source || "").toUpperCase())}) ↗</b></a>` : ""}
            ${autres}
            ${(ao.rectificatifs || []).length ? `<span class="sous">Rectificatifs : ${ao.rectificatifs.map(esc).join(", ")}</span>` : ""}
          </div>
          <dl class="faits">
            <div><dt>Date limite</dt><dd>${fmtDate(ao.date_limite)}</dd></div>
            <div><dt>Publication</dt><dd>${fmtDate(ao.date_publication, false)}</dd></div>
            <div><dt>Lieu</dt><dd>${esc(ao.lieu) || "—"}</dd></div>
            <div><dt>Visite</dt><dd>${ao.visite_obligatoire === true ? "Obligatoire" : ao.visite_obligatoire === false ? "Non obligatoire" : "À vérifier"}</dd></div>
            <div><dt>Procédure</dt><dd>${esc(ao.procedure) || "—"}</dd></div>
            <div><dt>Type</dt><dd>${esc((ao.type_marche || []).join(", ")) || "—"}</dd></div>
            <div><dt>Nature de l'avis</dt><dd>${esc((ao.nature || "").replace("_", " "))}</dd></div>
            <div><dt>CPV</dt><dd>${esc((ao.cpv || []).join(", ")) || "—"}</dd></div>
            ${ao.titulaire ? `<div><dt>Titulaire</dt><dd>${esc(ao.titulaire)}</dd></div>` : ""}
            ${ao.portail ? `<div><dt>Portail</dt><dd>${esc(ao.portail)}</dd></div>` : ""}
          </dl>
          ${(ao.score_detail || []).length ? `<p class="aide" style="margin-top:16px">Score : ${ao.score_detail.map(esc).join(" · ")}</p>` : ""}
        </div>

        <div class="carte synthese" id="bloc-synthese">${blocSynthese(ao)}</div>

        <div class="carte documents">
          <h2>Dossier de consultation (DCE)</h2>
          <p class="aide">Téléchargez le DCE sur la plateforme de l'acheteur puis déposez-le ici (ZIP, PDF, Word, Excel ; 50 Mo par fichier sur le forfait gratuit). Relancez ensuite la synthèse.</p>
          <ul>${(ao.documents || []).map((d, i) => `<li><a href="#" data-telecharger="${i}">${esc(d.nom)}</a>
            <span class="sous">${(d.taille / 1048576).toFixed(1)} Mo · ${esc(d.depose_par?.nom || "")} · ${fmtDate(d.depose_le, false)}
            <button class="btn-lien" data-supprimer="${i}">supprimer</button></span></li>`).join("") || `<li class="sous">Aucune pièce déposée.</li>`}</ul>
          <label class="depot" id="depot"><input type="file" id="fichiers" multiple hidden>
            Glissez des fichiers ici ou <u>parcourez</u><div class="progression" hidden><div></div></div></label>
        </div>

        <div class="carte">
          <h2>Notes de l'équipe</h2>
          <textarea id="notes" placeholder="Contacts, points de vigilance, partenaires de groupement envisagés…">${esc(ao.notes || "")}</textarea>
          <button class="btn btn-secondaire" id="sauver-notes" style="margin-top:8px">Enregistrer les notes</button>
        </div>

        <div class="carte"><h2>Historique</h2><ul class="historique" id="historique"><li class="sous">Chargement…</li></ul></div>
      </div>

      <aside class="colonne-actions">${blocActions(ao)}</aside>
    </div>`;

  brancherFiche(ao);
  if (nouvelleVue || !desabonnementHistorique || dernierHistorique.id !== id) chargerHistorique(id);
  else $("#historique").innerHTML = dernierHistorique.html;
}

function blocActions(ao) {
  const ouvert = !["abandonne", "gagne", "perdu"].includes(ao.statut);
  const options = Object.entries(STATUTS).map(([k, v]) => `<option value="${k}" ${ao.statut === k ? "selected" : ""}>${v}</option>`).join("");
  const membres = [...etat.membres.values()].map((m) => `<option value="${esc(m.email)}" ${ao.responsable?.email === m.email ? "selected" : ""}>${esc(m.nom || m.email)}</option>`).join("");
  const iaDiffere = ao.categorie_ia && ao.categorie_ia !== ao.categorie;
  return `<div class="carte">
    <h3>Suivi</h3>
    <div class="statut-courant">${esc(STATUTS[ao.statut] || ao.statut)}</div>
    <div class="sous">${ao.responsable ? `Responsable : ${nomCourt(ao.responsable)}` : "Personne n'a encore pris ce dossier."}</div>
    ${ao.statut === "abandonne" ? `<div class="motif"><b>${esc(MOTIFS[ao.motif_abandon] || ao.motif_abandon)}</b><br>${esc(ao.commentaire_abandon)}</div>` : ""}
    ${ao.statut === "a_qualifier" ? `<button class="btn" id="prendre">Je prends en charge</button>` : ""}
    ${ouvert ? `<button class="btn btn-danger" id="abandonner">Ne pas poursuivre…</button>` : ""}
    <label for="statut">Changer le statut</label>
    <select id="statut" style="width:100%">${options}</select>
    <label for="responsable">Responsable</label>
    <select id="responsable" style="width:100%"><option value="">—</option>${membres}</select>
    <label for="categorie">Type de réponse</label>
    <select id="categorie" style="width:100%">${Object.entries(CATEGORIES).map(([k, v]) => `<option value="${k}" ${ao.categorie === k ? "selected" : ""}>${v}</option>`).join("")}</select>
    ${iaDiffere ? `<p class="aide">L'analyse IA suggère : ${esc(CATEGORIES[ao.categorie_ia])}</p>` : ""}
    ${["depose", "gagne", "perdu"].includes(ao.statut) ? `<label for="montant">Montant déposé (€ HT)</label>
      <input type="number" id="montant" min="0" step="100" style="width:100%" value="${esc(ao.montant_depose ?? "")}">` : ""}
  </div>`;
}

function blocSynthese(ao) {
  const s = ao.synthese;
  const st = ao.synthese_statut;
  const bouton = `<button class="btn ${s ? "btn-secondaire" : ""}" id="lancer-synthese" ${st === "demandee" || st === "en_cours" ? "disabled" : ""}>${s ? "Relancer la synthèse" : "Lancer la synthèse IA"}</button>`;
  const attente = {
    demandee: `<p class="aide">Synthèse demandée : elle sera prête lors du prochain passage (au plus 30 minutes en journée ouvrée).</p>`,
    en_cours: `<p class="aide">Synthèse en cours de rédaction…</p>`,
    erreur: `<p class="erreur">La dernière synthèse a échoué : ${esc(ao.synthese_erreur)}</p>`,
  }[st] || "";
  if (!s) {
    return `<h2>Synthèse</h2><p class="aide">Aucune synthèse pour l'instant. Elle analyse l'avis et les pièces du DCE déposées : lots, visite, critères, risques, estimation de coût.</p>${attente}${bouton}`;
  }
  const liste = (t, xs) => (xs && xs.length ? `<section><h3>${t}</h3><ul>${xs.map((x) => `<li>${esc(x)}</li>`).join("")}</ul></section>` : "");
  const e = s.estimation_cout || {};
  const montant = e.montant_min_ht || e.montant_max_ht ? `${euros(e.montant_min_ht)} – ${euros(e.montant_max_ht)} HT` : "Non chiffrable en l'état";
  const lots = (s.lots || []).length ? `<section><h3>Lots</h3><table class="lots"><tr><th>Lot</th><th>Intitulé</th><th>Pertinence ADR</th><th>Commentaire</th></tr>
      ${s.lots.map((l) => `<tr><td>${esc(l.numero)}</td><td>${esc(l.intitule)}</td><td>${esc(l.pertinence_adr)}</td><td>${esc(l.commentaire)}</td></tr>`).join("")}</table></section>` : "";
  const criteres = (s.criteres_attribution || []).length ? `<section><h3>Critères d'attribution</h3><ul>${s.criteres_attribution.map((c) => `<li>${esc(c.critere)}${c.poids ? ` — <b>${esc(c.poids)}</b>` : ""}</li>`).join("")}</ul></section>` : "";
  return `<h2>Synthèse</h2>
    <p class="aide">Rédigée le ${fmtDate(ao.synthese_le)} à partir de : ${(ao.synthese_pieces || []).map(esc).join(", ")}. À vérifier sur les pièces officielles.</p>
    ${surAvisSeul(ao) ? `<p class="aide">Cette synthèse ne repose que sur l'avis de marché, sans pièces du DCE. Elle est lancée automatiquement pour les AO dont le score est d'au moins ${SEUIL_SYNTHESE}. Déposez le DCE puis relancez-la pour une analyse complète.</p>` : ""}
    <div class="reco reco-${esc(s.recommandation)}"><b>${esc(RECOS[s.recommandation] || s.recommandation)}</b><span>${esc(s.justification)}</span></div>
    <p>${esc(s.resume)}</p>
    ${liste("Points majeurs", s.points_majeurs)}
    ${lots}
    ${s.visite ? `<section><h3>Visite</h3><p>${s.visite.obligatoire === true ? "<b>Obligatoire.</b> " : s.visite.obligatoire === false ? "Non obligatoire. " : ""}${esc(s.visite.details)}</p></section>` : ""}
    <section><h3>Estimation de coût</h3><div class="estimation">
      <div class="montant">${montant}</div>
      <div class="sous">Fiabilité : <b>${esc(e.fiabilite)}</b> · Périmètre : ${esc(e.perimetre)}</div>
      <div class="sous">Base : ${esc(e.base)}</div>
      ${(e.hypotheses || []).length ? `<ul>${e.hypotheses.map((h) => `<li>${esc(h)}</li>`).join("")}</ul>` : ""}
    </div></section>
    ${criteres}
    ${s.planning ? `<section><h3>Planning</h3><p>${esc(s.planning)}</p></section>` : ""}
    ${liste("Exigences techniques", s.exigences_techniques)}
    ${liste("Pièces à fournir", s.pieces_a_fournir)}
    ${liste("Risques", s.risques)}
    ${liste("Questions à poser à l'acheteur", s.questions_acheteur)}
    ${liste("Informations manquantes", s.informations_manquantes)}
    ${attente}<div style="margin-top:16px">${bouton}</div>`;
}

function brancherFiche(ao) {
  const id = ao.id;
  const action = (fn) => async (...args) => { try { await fn(...args); } catch (e) { erreur(e); } };

  $("#prendre") && ($("#prendre").onclick = action(async () => {
    await majAO(id, { statut: "pris_en_charge", responsable: moi(), pris_en_charge_le: maintenantISO() }, "prise_en_charge", { par: moi().nom });
    toast("Dossier pris en charge.");
  }));

  $("#abandonner") && ($("#abandonner").onclick = () => dialogueAbandon(ao));

  $("#statut").onchange = action(async (e) => {
    const s = e.target.value;
    if (s === "abandonne") { e.target.value = ao.statut; return dialogueAbandon(ao); }
    const champs = { statut: s };
    if (s !== "a_qualifier" && !ao.responsable) champs.responsable = moi();
    if (["gagne", "perdu"].includes(s)) champs.decision_le = maintenantISO();
    await majAO(id, champs, "statut", { avant: ao.statut, apres: s });
    toast(`Statut : ${STATUTS[s]}`);
  });

  $("#responsable").onchange = action(async (e) => {
    const m = etat.membres.get(e.target.value);
    if (!m && ao.statut !== "a_qualifier") { e.target.value = ao.responsable?.email || ""; return toast("Un dossier en cours doit avoir un responsable."); }
    const r = m ? { email: m.email, nom: m.nom || m.email } : null;
    await majAO(id, { responsable: r }, "responsable", { nom: r?.nom || "aucun" });
    toast("Responsable mis à jour.");
  });

  $("#categorie").onchange = action(async (e) => {
    await majAO(id, { categorie: e.target.value, categorie_corrigee: true }, "categorie", { avant: ao.categorie, apres: e.target.value });
    toast("Type de réponse corrigé.");
  });

  $("#montant") && ($("#montant").onchange = action(async (e) => {
    const v = e.target.value === "" ? null : Number(e.target.value);
    await majAO(id, { montant_depose: v }, "montant", { montant: v });
    toast("Montant enregistré.");
  }));

  $("#sauver-notes").onclick = action(async () => {
    await majAO(id, { notes: $("#notes").value }, "notes");
    $("#notes").blur();
    router();
    toast("Notes enregistrées.");
  });

  $("#lancer-synthese") && ($("#lancer-synthese").onclick = action(async () => {
    await majAO(id, { synthese_statut: "demandee" }, "synthese_demandee", { pieces: (ao.documents || []).length });
    toast("Synthèse demandée.");
  }));

  // Documents
  $$("[data-telecharger]").forEach((a) => (a.onclick = action(async (e) => {
    e.preventDefault();
    const d = ao.documents[Number(a.dataset.telecharger)];
    const { data, error } = await sb.storage.from(BUCKET).createSignedUrl(d.chemin, 600);
    if (error) throw error;
    window.open(data.signedUrl, "_blank", "noopener");
  })));
  $$("[data-supprimer]").forEach((b) => (b.onclick = action(async () => {
    const d = ao.documents[Number(b.dataset.supprimer)];
    await sb.storage.from(BUCKET).remove([d.chemin]);
    await majAO(id, { documents: ao.documents.filter((x) => x.chemin !== d.chemin) }, "document_supprime", { nom: d.nom });
    toast("Pièce supprimée.");
  })));

  const depot = $("#depot");
  const envoyer = action(async (fichiers) => {
    const barre = $(".progression", depot);
    barre.hidden = false;
    const liste = [...fichiers];
    const nouveaux = [];
    for (const [i, f] of liste.entries()) {
      if (f.size > 50 * 1048576) toast(`${f.name} dépasse 50 Mo : il peut être refusé selon le forfait Supabase.`);
      const chemin = `aos/${id}/${Date.now()}_${f.name.replace(/[^\w.\-]+/g, "_")}`;
      $("div", barre).style.width = `${((i + 0.5) / liste.length) * 100}%`;
      const { error } = await sb.storage.from(BUCKET).upload(chemin, f, { upsert: false });
      if (error) throw error;
      nouveaux.push({ nom: f.name, chemin, taille: f.size, depose_par: moi(), depose_le: maintenantISO() });
    }
    $("div", barre).style.width = "100%";
    await majAO(id, { documents: [...(etat.aos.get(id)?.documents || []), ...nouveaux] }, "documents_deposes", { noms: nouveaux.map((n) => n.nom) });
    toast(`${nouveaux.length} pièce(s) déposée(s). Pensez à lancer la synthèse.`);
  });
  $("#fichiers").onchange = (e) => e.target.files.length && envoyer(e.target.files);
  depot.ondragover = (e) => { e.preventDefault(); depot.classList.add("survol"); };
  depot.ondragleave = () => depot.classList.remove("survol");
  depot.ondrop = (e) => { e.preventDefault(); depot.classList.remove("survol"); e.dataTransfer.files.length && envoyer(e.dataTransfer.files); };
}

function dialogueAbandon(ao) {
  const d = $("#dialogue");
  d.innerHTML = `<form method="dialog" id="form-abandon">
    <h2>Ne pas poursuivre cet appel d'offres</h2>
    <p class="aide">${esc(ao.titre)}</p>
    <label for="motif">Motif principal</label>
    <select id="motif" required><option value="">Choisir…</option>${Object.entries(MOTIFS).map(([k, v]) => `<option value="${k}">${esc(v)}</option>`).join("")}</select>
    <label for="commentaire">Explication (obligatoire)</label>
    <textarea id="commentaire" required minlength="10" placeholder="Ex. : visite le 12/11 incompatible avec le chantier Viparis ; lot menuiserie intégré au lot TCE…"></textarea>
    <p class="erreur" id="err-abandon" hidden></p>
    <div class="pied-dialogue"><button type="button" class="btn btn-secondaire" id="annuler">Annuler</button><button class="btn btn-danger" id="confirmer">Confirmer l'abandon</button></div>
  </form>`;
  d.showModal();
  $("#annuler").onclick = () => d.close();
  $("#form-abandon").onsubmit = async (e) => {
    e.preventDefault();
    const motif = $("#motif").value, commentaire = $("#commentaire").value.trim();
    const err = $("#err-abandon");
    if (!motif || commentaire.length < 10) { err.textContent = "Choisissez un motif et expliquez en quelques mots (10 caractères minimum)."; err.hidden = false; return; }
    try {
      await majAO(ao.id, {
        statut: "abandonne", motif_abandon: motif, commentaire_abandon: commentaire,
        responsable: ao.responsable || moi(), decision_le: maintenantISO(),
      }, "abandon", { motif: MOTIFS[motif], commentaire });
      d.close();
      toast("Abandon enregistré avec son motif.");
      router();
    } catch (x) { erreur(x); }
  };
}

const LIBELLES_EVT = {
  detection: (e) => `Détecté par la veille (score ${e.details?.score ?? "?"})`,
  modification: () => "Avis modifié à la source",
  rectificatif: (e) => `Rectificatif publié${e.details?.nouvelle_date_limite ? ` · nouvelle date limite ${fmtDate(e.details.nouvelle_date_limite)}` : ""}`,
  annulation: () => "Avis annulé par l'acheteur",
  prise_en_charge: (e) => `Pris en charge par ${esc(e.details?.par)}`,
  statut: (e) => `Statut : ${esc(STATUTS[e.details?.avant] || "?")} → <b>${esc(STATUTS[e.details?.apres] || "?")}</b>`,
  abandon: (e) => `<b>Abandon</b> — ${esc(e.details?.motif)} : ${esc(e.details?.commentaire)}`,
  responsable: (e) => `Responsable : ${esc(e.details?.nom)}`,
  categorie: (e) => `Type de réponse : ${esc(CATEGORIES[e.details?.apres])}`,
  notes: () => "Notes mises à jour",
  montant: (e) => `Montant déposé : ${euros(e.details?.montant)}`,
  documents_deposes: (e) => `Pièces déposées : ${esc((e.details?.noms || []).join(", "))}`,
  document_supprime: (e) => `Pièce supprimée : ${esc(e.details?.nom)}`,
  synthese_demandee: () => "Synthèse IA demandée",
  synthese: (e) => `Synthèse IA rédigée — recommandation : ${esc(RECOS[e.details?.recommandation] || "?")}`,
  creation_manuelle: () => "Ajouté manuellement",
};

async function afficherHistorique(id) {
  const { data, error } = await sb.from("evenements").select("*").eq("ao_id", id).order("date", { ascending: false }).limit(100);
  if (error) return erreur(error);
  const html = data.map((e) => {
    const lib = (LIBELLES_EVT[e.type] || (() => esc(e.type)))(e);
    const qui = e.par === "veille" ? "Veille automatique" : esc(etat.membres.get(e.par)?.nom || e.par);
    return `<li>${lib}<div class="quand">${fmtDate(e.date)} · ${qui}</div></li>`;
  }).join("") || `<li class="sous">Aucun événement.</li>`;
  dernierHistorique = { id, html };
  const ul = $("#historique");
  if (ul && decodeURIComponent(location.hash).endsWith(`/${id}`)) ul.innerHTML = html;
}

function chargerHistorique(id) {
  desabonnementHistorique?.();
  afficherHistorique(id);
  const canal = sb.channel(`historique-${id}`)
    .on("postgres_changes", { event: "INSERT", schema: "public", table: "evenements", filter: `ao_id=eq.${id}` }, () => afficherHistorique(id))
    .subscribe();
  desabonnementHistorique = () => sb.removeChannel(canal);
}

// --- Bilan --------------------------------------------------------------------------

async function vueBilan() {
  const aos = [...etat.aos.values()];
  const pertinents = aos.filter((a) => a.categorie !== "non_pertinente");
  const par = (cle, liste) => liste.reduce((m, a) => ((m[a[cle]] = (m[a[cle]] || 0) + 1), m), {});
  const statuts = par("statut", pertinents);
  const motifs = par("motif_abandon", aos.filter((a) => a.statut === "abandonne"));
  const gagnes = statuts.gagne || 0, perdus = statuts.perdu || 0;
  const jourMs = 86400000;
  const depuis30 = aos.filter((a) => versDate(a.detecte_le) > Date.now() - 30 * jourMs && a.categorie !== "non_pertinente").length;
  const barres = (obj, libelles, ordre) => {
    const max = Math.max(1, ...Object.values(obj));
    const entrees = ordre ? ordre.filter((k) => k in obj).map((k) => [k, obj[k]]) : Object.entries(obj).sort((a, b) => b[1] - a[1]);
    return entrees.map(([k, v]) => `<div class="ligne-barre"><span>${esc(libelles[k] || k)}</span>
      <div class="fond-barre"><div class="barre-val" style="width:${(v / max) * 100}%"></div></div><b>${v}</b></div>`).join("") || `<p class="sous">Pas encore de données.</p>`;
  };

  // Dossiers à traiter et échéances
  const aTraiter = pertinents.filter((a) => ["a_qualifier", "pris_en_charge", "en_redaction"].includes(a.statut)
    && !(a.statut === "a_qualifier" && delaiTropCourt(a)));
  const aQualifier = pertinents.filter((a) => a.statut === "a_qualifier" && !delaiTropCourt(a)).length;
  const manques = aos.filter((a) => estManque(a) && a.categorie !== "non_pertinente" && (a.score == null || a.score >= SEUIL_PERTINENT)).length;
  const echeances = aTraiter
    .map((a) => ({ a, j: joursRestants(a.date_limite) }))
    .filter((x) => x.j !== null && x.j >= 0)
    .sort((x, y) => x.j - y.j);
  const proches = echeances.filter((x) => x.j < SEUIL_CRITIQUE).length;
  const montantDepose = pertinents.filter((a) => a.statut === "depose").reduce((t, a) => t + (Number(a.montant_depose) || 0), 0);
  const montantGagne = pertinents.filter((a) => a.statut === "gagne").reduce((t, a) => t + (Number(a.montant_depose) || 0), 0);

  // Détections des 8 dernières semaines (0 = semaine en cours)
  const semaines = Array(8).fill(0);
  pertinents.forEach((a) => {
    const d = versDate(a.detecte_le);
    if (!d) return;
    const n = Math.floor((Date.now() - d) / (7 * jourMs));
    if (n >= 0 && n < 8) semaines[n] += 1;
  });
  const maxSem = Math.max(1, ...semaines);
  const histo = semaines.map((v, n) => ({ v, n })).reverse().map(({ v, n }) => `<div class="col-histo" title="${v} AO détecté(s) ${n === 0 ? "ces 7 derniers jours" : `il y a ${n} semaine${n > 1 ? "s" : ""}`}">
      <b>${v}</b><div class="tige" style="height:${Math.max(3, (v / maxSem) * 100)}%"></div><span>${n === 0 ? "7 j" : `-${n} s`}</span></div>`).join("");

  // Charge par responsable (dossiers en cours ou déposés)
  const charge = {};
  pertinents.filter((a) => ["pris_en_charge", "en_redaction", "depose"].includes(a.statut) && a.responsable)
    .forEach((a) => { const n = a.responsable.nom || a.responsable.email; charge[n] = (charge[n] || 0) + 1; });

  const lignesEcheances = echeances.slice(0, 6).map(({ a }) => `<a class="echeance" href="#/ao/${encodeURIComponent(a.id)}">
      <div><div class="titre-ao" title="${esc(a.titre)}">${esc(titreCourt(a))}</div><div class="sous">${esc(a.acheteur || "")}${a.responsable ? ` · ${nomCourt(a.responsable)}` : ""} · ${esc(STATUTS[a.statut] || a.statut)}</div></div>
      <div>${pastilleUrgence(a)}</div></a>`).join("");

  $("#app").innerHTML = `<h1>Bilan</h1>
    <div class="chiffres">
      <div class="chiffre"><div class="val">${depuis30}</div><div class="lib">AO pertinents détectés (30 j)</div></div>
      <div class="chiffre"><div class="val">${aQualifier}</div><div class="lib">À qualifier</div></div>
      <div class="chiffre ${proches ? "alerte" : ""}"><div class="val">${proches}</div><div class="lib">Échéances à moins de ${SEUIL_CRITIQUE} jours</div></div>
      <div class="chiffre"><div class="val">${(statuts.pris_en_charge || 0) + (statuts.en_redaction || 0)}</div><div class="lib">En cours</div></div>
      <div class="chiffre"><div class="val">${manques}</div><div class="lib">Laissés de côté faute de ressources</div></div>
      <div class="chiffre"><div class="val">${statuts.depose || 0}</div><div class="lib">Offres déposées en attente${montantDepose ? ` · ${euros(montantDepose)}` : ""}</div></div>
      <div class="chiffre"><div class="val">${gagnes + perdus ? Math.round((gagnes / (gagnes + perdus)) * 100) + " %" : "—"}</div><div class="lib">Taux de réussite (${gagnes} / ${gagnes + perdus})${montantGagne ? ` · ${euros(montantGagne)} gagnés` : ""}</div></div>
    </div>
    <div class="duo">
      <div class="carte"><h2>Prochaines échéances</h2>
        <div class="echeances">${lignesEcheances || `<p class="sous">Aucun dossier à traiter avec une date limite à venir.</p>`}</div>
        ${echeances.length > 6 ? `<p class="aide" style="margin-bottom:0">Et ${echeances.length - 6} autre${echeances.length - 6 > 1 ? "s" : ""} : voir la liste des AO.</p>` : ""}
      </div>
      <div class="carte"><h2>Détections par semaine</h2>
        <div class="histo">${histo}</div>
        <p class="aide" style="margin-bottom:0">AO pertinents détectés sur les 8 dernières semaines.</p>
      </div>
    </div>
    <div class="duo">
      <div class="carte"><h2>Parcours des dossiers</h2><div class="barres">${barres(statuts, STATUTS, ["a_qualifier", "pris_en_charge", "en_redaction", "depose", "gagne", "perdu", "abandonne"])}</div></div>
      <div class="carte"><h2>Pourquoi on ne poursuit pas</h2><div class="barres">${barres(motifs, MOTIFS)}</div></div>
    </div>
    <div class="duo">
      <div class="carte"><h2>Type de réponse</h2><div class="barres">${barres(par("categorie", pertinents), CATEGORIES)}</div></div>
      <div class="carte"><h2>Charge par personne</h2><div class="barres">${barres(charge, {})}</div>
        <p class="aide" style="margin-bottom:0">Dossiers pris en charge, en rédaction ou déposés.</p></div>
    </div>
    ${etat.utilisateur.role === "admin" ? `<div class="carte"><h2>Lancer maintenant</h2>
      <p class="aide">La collecte tourne automatiquement chaque jour ouvré à 6 h 17. Vous pouvez aussi la lancer à la demande.</p>
      <div style="display:flex;gap:12px;align-items:center;flex-wrap:wrap">
        <button class="btn" id="lancer-collecte">Lancer une collecte</button>
        <label class="case" style="margin:0"><input type="checkbox" id="lancer-recap"> envoyer aussi le récapitulatif par mail</label>
        <button class="btn btn-secondaire" id="lancer-syntheses">Lancer les synthèses en attente</button>
      </div>
      <h3 style="margin-top:18px">Dernières exécutions</h3><div id="executions" class="sous">Chargement…</div>
    </div>` : ""}
    <div class="carte"><h2>Dernières collectes</h2><div id="collectes" class="sous">Chargement…</div></div>`;
  if (etat.utilisateur.role === "admin") {
    $("#lancer-collecte").onclick = () => lancerWorkflow("collecte", $("#lancer-recap").checked);
    $("#lancer-syntheses").onclick = () => lancerWorkflow("synthese");
    afficherExecutions();
    Promise.all(["collecte", "synthese"].map((w) => appelerFonction({ action: "statut", workflow: w })
      .then((d) => { etat.executions = { ...(etat.executions || {}), [w]: d.executions || [] }; })))
      .then(afficherExecutions)
      .catch((e) => { const el = $("#executions"); if (el) el.innerHTML = `<p class="erreur">${esc(e.message)}</p>`; });
  }

  try {
    const { data: collectes, error } = await sb.from("collectes").select("*").order("id", { ascending: false }).limit(8);
    if (error) throw error;
    const el = $("#collectes");
    if (!el) return;
    el.innerHTML = collectes.length ? `<table class="lots"><tr><th>Date</th><th>BOAMP lus</th><th>TED lus</th><th>Nouveaux</th><th>Modifiés</th><th>Obstacles</th></tr>
      ${collectes.map((c) => { return `<tr><td>${fmtDate(c.debut)}</td><td>${c.sources?.boamp_lus ?? "—"}</td><td>${c.sources?.ted_lus ?? "—"}</td>
        <td>${c.resultats?.nouveau ?? 0}</td><td>${(c.resultats?.modifie ?? 0) + (c.resultats?.rattache ?? 0)}</td>
        <td>${(c.erreurs || []).map(esc).join("<br>") || "Aucun"}</td></tr>`; }).join("")}</table>` : "Aucune collecte enregistrée.";
  } catch (e) { erreur(e); }
}

// --- Lancement manuel de la collecte (administrateurs) --------------------------------

async function appelerFonction(corps) {
  const { data, error } = await sb.functions.invoke("lancer-collecte", { body: corps });
  if (error) {
    let msg = error.message;
    try { const d = await error.context.json(); msg = d.erreur || msg; } catch { /* réponse non JSON */ }
    if (/Failed to send|not found|404/i.test(msg)) msg = "La fonction « lancer-collecte » n'est pas encore déployée dans Supabase.";
    throw new Error(msg);
  }
  return data;
}

let suiviExecutions = null;

async function lancerWorkflow(workflow, recap = false) {
  try {
    await appelerFonction({ action: "lancer", workflow, recap });
    toast(workflow === "collecte" ? "Collecte lancée : comptez 1 à 3 minutes." : "Synthèses lancées.");
    suivreExecutions(workflow);
  } catch (e) { erreur(e); }
}

// Interroge l'état des exécutions jusqu'à la fin, puis recharge les AO
function suivreExecutions(workflow = "collecte") {
  clearInterval(suiviExecutions);
  let tours = 0, vuEnCours = false;
  const tick = async () => {
    tours += 1;
    try {
      const { executions = [] } = await appelerFonction({ action: "statut", workflow });
      etat.executions = { ...(etat.executions || {}), [workflow]: executions };
      const enCours = executions.some((x) => x.statut !== "completed");
      vuEnCours ||= enCours;
      afficherExecutions();
      if ((vuEnCours && !enCours) || tours > 40) {
        clearInterval(suiviExecutions);
        if (vuEnCours) {
          await chargerAOs();
          router();
          const derniere = executions[0];
          toast(derniere?.conclusion === "success" ? "Terminé : la liste est à jour." : "Exécution terminée avec une erreur : voir le détail sur GitHub.");
        }
      }
    } catch (e) { clearInterval(suiviExecutions); erreur(e); }
  };
  setTimeout(tick, 4000);
  suiviExecutions = setInterval(tick, 15000);
}

function afficherExecutions() {
  const el = $("#executions");
  if (!el) return;
  const lib = (x) => x.statut === "queued" ? "En attente" : x.statut === "in_progress" ? "En cours…"
    : x.conclusion === "success" ? "Réussie" : x.conclusion === "cancelled" ? "Annulée" : "Échec";
  const cls = (x) => x.statut !== "completed" ? "p-urgente" : x.conclusion === "success" ? "p-normale" : "p-critique";
  const lignes = Object.entries(etat.executions || {}).flatMap(([w, xs]) => xs.slice(0, 3).map((x) => ({ w, ...x })))
    .sort((a, b) => String(b.debut).localeCompare(String(a.debut)));
  el.innerHTML = lignes.length ? `<table class="lots"><tr><th>Quoi</th><th>Lancée</th><th>Par</th><th>État</th><th></th></tr>
    ${lignes.map((x) => `<tr><td>${x.w === "collecte" ? "Collecte" : "Synthèses"}</td><td>${fmtDate(x.debut)}</td>
      <td>${x.declencheur === "schedule" ? "Planification" : "Manuel"}</td><td><span class="pastille ${cls(x)}">${lib(x)}</span></td>
      <td><a href="${urlSure(x.url)}" target="_blank" rel="noopener">détail ↗</a></td></tr>`).join("")}</table>`
    : `<p class="sous">Aucune exécution récente.</p>`;
}

// --- Équipe (administrateurs) ------------------------------------------------------------

function vueEquipe() {
  if (etat.utilisateur.role !== "admin") { $("#app").innerHTML = `<p class="vide">Réservé aux administrateurs.</p>`; return; }
  const membres = [...etat.membres.values()].sort((a, b) => (a.nom || a.email).localeCompare(b.nom || b.email));
  $("#app").innerHTML = `<h1>Équipe</h1>
    <div class="carte formulaire">
      <p class="aide">Pour donner accès à quelqu'un : 1) créez son compte dans Supabase (<b>Authentication → Users → Add user → Create new user</b>,
        avec un mot de passe provisoire et « Auto Confirm User » coché) ; 2) ajoutez-le ici avec la même adresse. Pour retirer un accès, retirez-le ici
        et supprimez son compte dans Supabase. Cliquez sur un nom pour le corriger.</p>
      <div class="tableau" style="margin-bottom:16px"><table><thead><tr><th>Nom</th><th>Adresse</th><th>Rôle</th><th></th></tr></thead><tbody>
        ${membres.map((m) => `<tr><td><input value="${esc(m.nom)}" data-nom="${esc(m.email)}" aria-label="Nom" style="width:100%"></td><td>${esc(m.email)}</td><td>${m.role === "admin" ? "Administrateur" : "Membre"}</td>
          <td>${m.email !== etat.utilisateur.email ? `<button class="btn-lien" data-retirer="${esc(m.email)}">retirer</button>` : ""}</td></tr>`).join("")}
      </tbody></table></div>
      <h2>Ajouter une personne</h2>
      <form id="form-membre" class="deux">
        <div><label for="m-nom">Nom</label><input id="m-nom" required></div>
        <div><label for="m-mail">Adresse mail</label><input id="m-mail" type="email" required></div>
        <div><label for="m-role">Rôle</label><select id="m-role"><option value="membre">Membre</option><option value="admin">Administrateur</option></select></div>
        <div style="align-self:end"><button class="btn">Ajouter</button></div>
      </form>
    </div>
`;
  $("#form-membre").onsubmit = async (e) => {
    e.preventDefault();
    const email = $("#m-mail").value.trim().toLowerCase();
    try {
      const { error } = await sb.from("membres").insert({ email, nom: $("#m-nom").value.trim(), role: $("#m-role").value });
      if (error) throw error;
      await chargerMembres();
      vueEquipe();
      toast(`${email} ajouté(e).`);
    } catch (x) { erreur(x); }
  };
  $$("[data-nom]").forEach((i) => (i.onchange = async () => {
    const nom = i.value.trim();
    if (!nom) { i.value = etat.membres.get(i.dataset.nom)?.nom || ""; return; }
    const { data, error } = await sb.from("membres").update({ nom }).eq("email", i.dataset.nom).select("email");
    if (error) return erreur(error);
    if (!data?.length) return toast("Modification refusée.");
    await chargerMembres();
    if (i.dataset.nom === etat.utilisateur.email) { etat.utilisateur.nom = nom; $("#utilisateur").textContent = nom; }
    i.blur();
    toast("Nom mis à jour.");
  }));
  $$("[data-retirer]").forEach((b) => (b.onclick = async () => {
    const { error } = await sb.from("membres").delete().eq("email", b.dataset.retirer);
    if (error) return erreur(error);
    await chargerMembres();
    vueEquipe();
    toast("Accès retiré.");
  }));
}

const OUVRAGES_DEFAUT = [
  ["Banque d'accueil", "ml"], ["Habillage mural bois", "m²"], ["Claustra bois", "m²"], ["Bloc-porte bois", "u"],
  ["Placard aménagé", "ml"], ["Bar / comptoir", "ml"], ["Cuisine sur mesure", "ml"], ["Stand d'exposition", "m²"],
  ["Vitrine / mobilier muséographique", "u"],
].map(([nom, unite]) => ({ nom, unite, prix_min: null, prix_max: null, note: "" }));

function vueReglages() {
  if (etat.utilisateur.role !== "admin") { $("#app").innerHTML = `<p class="vide">Réservé aux administrateurs.</p>`; return; }
  $("#app").innerHTML = `<h1>Réglages</h1>
    <div class="carte formulaire">
      <h2>Récapitulatif du lundi</h2>
      <p class="aide">Adresses qui reçoivent chaque lundi matin le compte rendu de la veille : nouveaux AO, échéances,
        sources vérifiées et obstacles. Une adresse par ligne ; elles peuvent être extérieures à l'équipe.</p>
      <textarea id="recap-liste" placeholder="lpernelle@latelierdurenard.com">Chargement…</textarea>
      <p class="erreur" id="err-recap" hidden></p>
      <div style="display:flex;gap:14px;align-items:center;margin-top:8px">
        <button class="btn" id="sauver-recap">Enregistrer les destinataires</button>
        <button class="btn-lien" id="recap-equipe" type="button">Ajouter toute l'équipe</button>
      </div>
      <p class="aide" id="recap-maj"></p>
    </div>
    <div class="carte" id="bloc-prix" style="max-width:980px">
      <h2>Prix de référence ADR</h2>
      <p class="aide">Utilisés par la synthèse IA pour estimer le coût d'un appel d'offres. Montants en euros HT, fourniture et pose.
        Laissez vide ce que vous ne savez pas : l'estimation l'indiquera comme moins fiable.</p>
      <p class="chargement">Chargement…</p>
    </div>`;
  chargerRecap();
  chargerPrix();
}

function lignePrix(o, i) {
  return `<tr data-ligne="${i}">
    <td><input data-champ="nom" value="${esc(o.nom)}" placeholder="Ouvrage" style="width:100%"></td>
    <td><select data-champ="unite">${["ml", "m²", "u", "forfait", "jour"].map((u) => `<option ${o.unite === u ? "selected" : ""}>${u}</option>`).join("")}</select></td>
    <td><input data-champ="prix_min" type="number" min="0" step="10" value="${o.prix_min ?? ""}" style="width:110px"></td>
    <td><input data-champ="prix_max" type="number" min="0" step="10" value="${o.prix_max ?? ""}" style="width:110px"></td>
    <td><input data-champ="note" value="${esc(o.note || "")}" placeholder="ex. chêne massif, vernis mat" style="width:100%"></td>
    <td><button class="btn-lien" data-suppr="${i}" type="button" title="Supprimer la ligne">✕</button></td>
  </tr>`;
}

async function chargerPrix() {
  const { data, error } = await sb.from("parametres").select("valeur,maj_par,maj_le").eq("cle", "referentiel_prix").maybeSingle();
  const bloc = $("#bloc-prix");
  if (!bloc) return;
  if (error) { $(".chargement", bloc).outerHTML = `<p class="erreur">Réglage indisponible : lancez le script supabase/002_parametres.sql dans Supabase.</p>`; return; }
  const v = data?.valeur || {};
  let ouvrages = Array.isArray(v.ouvrages) && v.ouvrages.length ? v.ouvrages : OUVRAGES_DEFAUT.map((o) => ({ ...o }));
  const num = (x) => (x === "" || x === null || x === undefined ? null : Number(x));
  const champ = (id, lib, val, aide) => `<div><label for="${id}">${lib}</label><input id="${id}" type="number" min="0" step="any" value="${val ?? ""}" style="width:100%"><div class="aide">${aide}</div></div>`;

  const dessiner = () => {
    $(".chargement, #contenu-prix", bloc)?.remove();
    bloc.insertAdjacentHTML("beforeend", `<div id="contenu-prix">
      <div class="formulaire" style="max-width:none"><div style="display:grid;grid-template-columns:repeat(4,1fr);gap:0 16px">
        ${champ("p-atelier", "Taux horaire atelier", v.taux_horaire_atelier, "€ HT / heure")}
        ${champ("p-pose", "Taux horaire pose", v.taux_horaire_pose, "€ HT / heure")}
        ${champ("p-fg", "Coefficient frais généraux", v.coefficient_frais_generaux, "ex. 1,25")}
        ${champ("p-marge", "Marge cible", v.marge_cible, "en %, ex. 15")}
      </div></div>
      <div class="tableau" style="margin-top:16px"><table><thead><tr><th>Ouvrage</th><th>Unité</th><th>Prix min</th><th>Prix max</th><th>Remarque</th><th></th></tr></thead>
        <tbody>${ouvrages.map(lignePrix).join("")}</tbody></table></div>
      <button class="btn-lien" id="ajout-ouvrage" type="button" style="margin-top:10px">+ Ajouter un ouvrage</button>
      <label for="p-remarques">Habitudes de chiffrage</label>
      <textarea id="p-remarques" placeholder="Délais d'atelier, finitions sous-traitées, zones d'intervention, minimum de commande…">${esc(v.remarques || "")}</textarea>
      <p class="erreur" id="err-prix" hidden></p>
      <button class="btn" id="sauver-prix" style="margin-top:10px">Enregistrer les prix</button>
      <p class="aide">${data?.maj_par ? `Modifié par ${esc(etat.membres.get(data.maj_par)?.nom || data.maj_par)} le ${fmtDate(data.maj_le)}.` : "Jamais enregistré."}</p>
    </div>`);
    const lire = () => $$("tbody tr[data-ligne]", bloc).map((tr) => {
      const o = {};
      $$("[data-champ]", tr).forEach((c) => (o[c.dataset.champ] = c.value.trim()));
      return { nom: o.nom, unite: o.unite, prix_min: num(o.prix_min), prix_max: num(o.prix_max), note: o.note };
    });
    $("#ajout-ouvrage").onclick = () => { ouvrages = [...lire(), { nom: "", unite: "u", prix_min: null, prix_max: null, note: "" }]; garder(); dessiner(); };
    $$("[data-suppr]", bloc).forEach((b) => (b.onclick = () => { ouvrages = lire().filter((_, i) => i !== Number(b.dataset.suppr)); garder(); dessiner(); }));
    const garder = () => Object.assign(v, {
      taux_horaire_atelier: num($("#p-atelier").value), taux_horaire_pose: num($("#p-pose").value),
      coefficient_frais_generaux: num($("#p-fg").value), marge_cible: num($("#p-marge").value), remarques: $("#p-remarques").value,
    });
    $("#sauver-prix").onclick = async () => {
      garder();
      const liste = lire().filter((o) => o.nom);
      const err = $("#err-prix");
      const incoherent = liste.find((o) => o.prix_min !== null && o.prix_max !== null && o.prix_min > o.prix_max);
      if (incoherent) { err.textContent = `« ${incoherent.nom} » : le prix min dépasse le prix max.`; err.hidden = false; return; }
      err.hidden = true;
      const valeur = { ...v, ouvrages: liste };
      const { data: ok, error: e } = await sb.from("parametres").upsert({ cle: "referentiel_prix", valeur }).select("cle");
      if (e) return erreur(e);
      if (!ok?.length) return toast("Modification refusée : réservé aux administrateurs.");
      ouvrages = liste;
      document.activeElement?.blur();
      toast("Prix enregistrés : ils serviront aux prochaines synthèses.");
    };
  };
  dessiner();
}

const MAIL = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

async function chargerRecap() {
  const { data, error } = await sb.from("parametres").select("valeur,maj_par,maj_le").eq("cle", "recap_destinataires").maybeSingle();
  const zone = $("#recap-liste");
  if (!zone) return;
  if (error) {
    zone.value = "";
    $("#err-recap").textContent = "Réglage indisponible : lancez le script supabase/002_parametres.sql dans Supabase.";
    $("#err-recap").hidden = false;
    return;
  }
  zone.value = (data?.valeur || []).join("\n");
  if (data?.maj_par) $("#recap-maj").textContent = `Modifié par ${etat.membres.get(data.maj_par)?.nom || data.maj_par} le ${fmtDate(data.maj_le)}.`;

  $("#recap-equipe").onclick = () => {
    const actuels = zone.value.split(/[\s,;]+/).filter(Boolean);
    const tous = [...new Set([...actuels, ...[...etat.membres.keys()]])];
    zone.value = tous.join("\n");
  };
  $("#sauver-recap").onclick = async () => {
    const liste = [...new Set(zone.value.split(/[\s,;]+/).map((x) => x.trim().toLowerCase()).filter(Boolean))];
    const invalides = liste.filter((x) => !MAIL.test(x));
    const err = $("#err-recap");
    if (invalides.length) { err.textContent = `Adresse(s) invalide(s) : ${invalides.join(", ")}`; err.hidden = false; return; }
    if (!liste.length) { err.textContent = "Indiquez au moins une adresse."; err.hidden = false; return; }
    err.hidden = true;
    const { data: ok, error: e } = await sb.from("parametres").upsert({ cle: "recap_destinataires", valeur: liste }).select("cle");
    if (e) return erreur(e);
    if (!ok?.length) return toast("Modification refusée : réservé aux administrateurs.");
    zone.value = liste.join("\n");
    zone.blur();
    toast(`${liste.length} destinataire(s) enregistré(s).`);
  };
}

// --- Ajout manuel -----------------------------------------------------------------------

function vueAjout() {
  $("#app").innerHTML = `<a class="retour" href="#/">← Retour</a><h1>Ajouter un appel d'offres</h1>
    <p class="aide">Pour les consultations repérées sur un portail fermé (RATP, SNCF, ADP, PLACE, Maximilien, Viparis…) ou reçues directement.</p>
    <form class="carte formulaire" id="form-ajout">
      <label for="a-titre">Objet *</label><input id="a-titre" required minlength="4">
      <div class="deux">
        <div><label for="a-acheteur">Acheteur / donneur d'ordre *</label><input id="a-acheteur" required></div>
        <div><label for="a-portail">Portail ou canal</label><input id="a-portail" list="portails" placeholder="RATP, SNCF, PLACE…">
          <datalist id="portails">${["PLACE", "Maximilien", "RATP", "SNCF", "Groupe ADP", "Île-de-France Mobilités", "Société des grands projets", "Paris Habitat", "Viparis", "GL events", "FFT", "Achatpublic", "AWS", "Mail direct"].map((p) => `<option value="${p}">`).join("")}</datalist></div>
        <div><label for="a-limite">Date limite *</label><input id="a-limite" type="datetime-local" required></div>
        <div><label for="a-lieu">Lieu</label><input id="a-lieu" placeholder="Paris 15e, 93…"></div>
        <div><label for="a-url">Lien vers l'avis</label><input id="a-url" type="url" placeholder="https://"></div>
        <div><label for="a-cat">Type de réponse</label><select id="a-cat">${Object.entries(CATEGORIES).map(([k, v]) => `<option value="${k}">${v}</option>`).join("")}</select></div>
        <div><label for="a-visite">Visite</label><select id="a-visite"><option value="">À vérifier</option><option value="oui">Obligatoire</option><option value="non">Non obligatoire</option></select></div>
      </div>
      <label for="a-desc">Description / texte de l'avis</label>
      <textarea id="a-desc" placeholder="Collez ici le texte de l'avis : il servira à la synthèse IA."></textarea>
      <label class="case" style="margin-top:12px"><input type="checkbox" id="a-moi" checked> Je prends ce dossier en charge</label>
      <div style="margin-top:16px"><button class="btn">Enregistrer</button></div>
    </form>`;
  $("#form-ajout").onsubmit = async (e) => {
    e.preventDefault();
    const id = `manuel-${Date.now()}`;
    const url = $("#a-url").value.trim();
    const visite = $("#a-visite").value;
    const prendre = $("#a-moi").checked;
    const data = {
      id, source: "manuel", ref: id.slice(7), titre: $("#a-titre").value.trim(), url: url || null,
      acheteur: $("#a-acheteur").value.trim(), portail: $("#a-portail").value.trim(), lieu: $("#a-lieu").value.trim(),
      date_limite: new Date($("#a-limite").value).toISOString(), date_publication: null,
      description: $("#a-desc").value.trim(), nature: "appel_offre", type_marche: [], cpv: [],
      categorie: $("#a-cat").value, categorie_corrigee: true, score: null, score_detail: [],
      statut: prendre ? "pris_en_charge" : "a_qualifier", responsable: prendre ? moi() : null,
      visite_obligatoire: visite === "" ? null : visite === "oui",
      nouveaute: "nouveau", modifie: false, documents: [], rectificatifs: [], synthese_statut: "aucune",
    };
    try {
      const { data: cree, error } = await sb.from("aos").insert(data).select(COLONNES);
      if (error) throw error;
      etat.aos.set(id, cree[0]);
      await sb.from("evenements").insert({ ao_id: id, type: "creation_manuelle", details: { portail: data.portail }, par: etat.utilisateur.email });
      location.hash = `#/ao/${id}`;
      toast("Appel d'offres ajouté.");
    } catch (x) { erreur(x); }
  };
}

// --- Démarrage ---------------------------------------------------------------------------

// La session est récupérée par supabase-js (lien magique ou session mémorisée).
