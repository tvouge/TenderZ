// Fonction Supabase « lancer-collecte »
//
// Permet aux administrateurs de lancer, depuis le site, la collecte ou les
// synthèses (workflows GitHub Actions) et d'en suivre l'avancement.
// Le jeton GitHub reste ici, côté serveur : il n'est jamais envoyé au navigateur.
//
// Secrets à définir (Supabase → Edge Functions → Secrets) :
//   GITHUB_TOKEN   jeton GitHub « fine-grained », accès au seul dépôt, permission Actions : Read and write
//   GITHUB_REPO    ex. Maximeperso0505/Tenders
//   GITHUB_BRANCH  facultatif, « main » par défaut
//   CLE_PUBLIQUE   facultatif : la clé publishable, seulement si l'appel est_admin échoue
// SUPABASE_URL et SUPABASE_ANON_KEY sont fournis automatiquement par Supabase.

const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
};
const WORKFLOWS: Record<string, string> = { collecte: "collecte.yml", synthese: "synthese.yml" };

const json = (corps: unknown, status = 200) =>
  new Response(JSON.stringify(corps), { status, headers: { ...CORS, "Content-Type": "application/json" } });

const env = (nom: string) => Deno.env.get(nom) ?? "";

async function estAdmin(auth: string): Promise<boolean> {
  const rest = env("SUPABASE_REST_URL") || `${env("SUPABASE_URL")}/rest/v1`;
  const r = await fetch(`${rest}/rpc/est_admin`, {
    method: "POST",
    // SUPABASE_ANON_KEY est fournie par Supabase ; CLE_PUBLIQUE (clé publishable) sert de secours
    // si les anciennes clés ont été désactivées dans le projet.
    headers: { apikey: env("CLE_PUBLIQUE") || env("SUPABASE_ANON_KEY"), Authorization: auth, "Content-Type": "application/json" },
    body: "{}",
  });
  return r.ok && (await r.json()) === true;
}

export async function traiter(req: Request): Promise<Response> {
  if (req.method === "OPTIONS") return new Response("ok", { headers: CORS });
  if (req.method !== "POST") return json({ erreur: "Méthode non autorisée" }, 405);

  const auth = req.headers.get("Authorization") ?? "";
  if (!auth.startsWith("Bearer ") || !(await estAdmin(auth))) {
    return json({ erreur: "Réservé aux administrateurs" }, 403);
  }

  const { action = "statut", workflow = "collecte", recap = false } = await req.json().catch(() => ({}));
  const fichier = WORKFLOWS[workflow];
  if (!fichier) return json({ erreur: `Workflow inconnu : ${workflow}` }, 400);

  const token = env("GITHUB_TOKEN"), depot = env("GITHUB_REPO");
  if (!token || !depot) return json({ erreur: "Secrets GITHUB_TOKEN et GITHUB_REPO à définir dans Supabase (Edge Functions → Secrets)" }, 500);

  const api = env("GITHUB_API") || "https://api.github.com";
  const gh = (chemin: string, init: RequestInit = {}) =>
    fetch(`${api}/repos/${depot}/actions/workflows/${fichier}${chemin}`, {
      ...init,
      headers: {
        Authorization: `Bearer ${token}`,
        Accept: "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "tenders-adr",
        "Content-Type": "application/json",
      },
    });

  if (action === "lancer") {
    const inputs = workflow === "collecte" ? { recap: recap ? "true" : "false" } : {};
    const r = await gh("/dispatches", { method: "POST", body: JSON.stringify({ ref: env("GITHUB_BRANCH") || "main", inputs }) });
    if (r.status !== 204) {
      const detail = await r.text();
      const aide = r.status === 401 ? "jeton GitHub invalide ou expiré"
        : r.status === 403 ? "le jeton n'a pas la permission Actions : Read and write sur ce dépôt"
        : r.status === 404 ? "dépôt ou workflow introuvable (vérifiez GITHUB_REPO)"
        : r.status === 422 ? "branche introuvable ou workflow sans déclenchement manuel" : "";
      return json({ erreur: `GitHub a refusé (${r.status})${aide ? ` : ${aide}` : ""}`, detail: detail.slice(0, 300) }, 502);
    }
    return json({ ok: true });
  }

  // action « statut » : dernières exécutions
  const r = await gh("/runs?per_page=5");
  if (!r.ok) return json({ erreur: `GitHub (${r.status})`, detail: (await r.text()).slice(0, 300) }, 502);
  const data = await r.json();
  return json({
    executions: (data.workflow_runs ?? []).map((x: Record<string, string>) => ({
      statut: x.status,          // queued | in_progress | completed
      conclusion: x.conclusion,  // success | failure | cancelled | null
      debut: x.run_started_at ?? x.created_at,
      maj: x.updated_at,
      declencheur: x.event,      // schedule | workflow_dispatch
      url: x.html_url,
    })),
  });
}

if (import.meta.main) Deno.serve(traiter);
