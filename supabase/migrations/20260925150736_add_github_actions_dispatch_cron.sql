create extension if not exists pg_cron with schema pg_catalog;
create extension if not exists pg_net with schema extensions;

-- The token itself is created out-of-band in Vault. Never commit it.
-- Required Vault secret: github_actions_dispatch_token
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
  '*/5 * * * *',
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
