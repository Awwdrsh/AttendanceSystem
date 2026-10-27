"""Demo data for Hajiri (port of apps/api/src/db/demo-suite and the CLI's seed-demo-suite command).

`manage.py seed_demo_suite --database <url> --yes [--company DEMO] [--only a,b]` fills every HR module of one company
with realistic sample data through the same service functions the pages use. Until 'demo' is in INSTALLED_APPS the same
command runs as `python -m demo --database <url> --yes` from server/.
"""
