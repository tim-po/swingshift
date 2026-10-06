"""Email allowlist authorization for control-plane administrators."""
def is_owner(account, cfg):
    return bool(account and account.status == 'active' and
                account.email.strip().lower() in {e.strip().lower() for e in cfg.owner_emails})
