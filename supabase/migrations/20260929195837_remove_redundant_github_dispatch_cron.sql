-- GitHub Actions owns the hourly :50 schedule. Remove the older five-minute
-- Supabase dispatcher so the same workflow is not launched twice.
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
