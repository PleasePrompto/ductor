# Scopewise Ticket Router

This wrapper runs the deterministic Ductor-side Scopewise ticket intake module from the local repo checkout.

Typical dry-run validation:

```bash
python3 tools/user_tools/scopewise_ticket_router.py \
  --event-file tools/user_tools/scopewise_ticket_router.manual.example.json \
  --dry-run
```

Live create/update uses the same command without `--dry-run`.

State files:

- local dedupe bindings: `workspace/software_factory/scopewise_ticket_router/dedupe_bindings.sqlite3`
- local audit trail: `workspace/software_factory/scopewise_ticket_router/audit/scopewise_ticket_router.jsonl`

The first slice accepts normalized JSON events only. Webhook or task wiring can call this same wrapper later.
