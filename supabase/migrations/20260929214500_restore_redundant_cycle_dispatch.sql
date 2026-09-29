create extension if not exists pg_cron with schema pg_catalog;
create extension if not exists pg_net with schema extensions;

-- GitHub's native schedule is best-effort and may start late. Dispatch at
-- :40 and :50 from Supabase as an independent scheduler, while predict.yml
-- keeps :45 and :55 fallbacks. The pipeline's stable Idempotency-Key makes
-- overlapping attempts safe and prevents duplicate official submissions.
do $do$
declare
  existing_job_id bigint;
begin
  select jobid
    into existing_job_id
  from cron.job
  where jobname = 'pulso-transmi-github-dispatch';

  if existing_job_id is not null then
    perform cron.unschedule(existing_job_id);
  end if;
end
$do$;

select cron.schedule(
  'pulso-transmi-github-dispatch',
  '40,50 * * * *',
  $cron$
    select net.http_post(
      url := 'https://api.github.com/repos/lunalugoeuc-a11y/pulso-transmi-sdk/actions/workflows/predict.yml/dispatches',
      headers := jsonb_build_object(
        'Accept', 'application/vnd.github+json',
        'Authorization', 'Bearer ' || (
          select decrypted_secret
          from vault.decrypted_secrets
          where name = 'github_actions_dispatch_token'
          limit 1
        ),
        'X-GitHub-Api-Version', '2022-11-28',
        'Content-Type', 'application/json',
        'User-Agent', 'pulso-transmi-supabase-cron'
      ),
      body := '{"ref":"main"}'::jsonb,
      timeout_milliseconds := 10000
    );
  $cron$
);
