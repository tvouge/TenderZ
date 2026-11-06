-- =====================================================================
--  Veille AO — ajout des paramètres modifiables depuis le site
--  (destinataires du récapitulatif du lundi).
--  À lancer une fois dans Supabase → SQL Editor, après schema.sql.
--  Peut être relancé sans risque.
-- =====================================================================

create table if not exists public.parametres (
  cle     text primary key,
  valeur  jsonb not null,
  maj_par text,
  maj_le  timestamptz not null default now()
);

create or replace function public.parametres_auteur() returns trigger
language plpgsql as $$
begin
  if current_user = 'authenticated' then
    new.maj_par := public.email_courant();
    new.maj_le  := now();
  end if;
  return new;
end $$;

drop trigger if exists parametres_auteur on public.parametres;
create trigger parametres_auteur before insert or update on public.parametres
  for each row execute function public.parametres_auteur();

alter table public.parametres enable row level security;
revoke all on public.parametres from anon, authenticated;
grant select, insert, update on public.parametres to authenticated;

drop policy if exists parametres_lecture on public.parametres;
create policy parametres_lecture on public.parametres for select to authenticated using (public.est_membre());
drop policy if exists parametres_gestion on public.parametres;
create policy parametres_gestion on public.parametres for all to authenticated
  using (public.est_admin()) with check (public.est_admin());

-- Valeur de départ (modifiable ensuite depuis la page Équipe)
insert into public.parametres (cle, valeur)
values ('recap_destinataires', '["lpernelle@latelierdurenard.com"]')
on conflict (cle) do nothing;
