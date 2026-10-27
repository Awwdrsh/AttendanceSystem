"""`python -m demo --database <url> --yes [--company DEMO] [--only a,b]`: the seed_demo_suite command without needing
'demo' in INSTALLED_APPS."""
import os
import sys


def main(argv: list[str]) -> None:
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
    import django
    django.setup()
    from django.core.management import call_command
    from django.core.management.base import CommandError

    from demo.management.commands.seed_demo_suite import Command
    cmd = Command()
    parser = cmd.create_parser('python -m demo', 'seed_demo_suite')
    opts = vars(parser.parse_args(argv))
    try:
        call_command(cmd, **{k: v for k, v in opts.items() if k in ('database', 'yes', 'company', 'only')})
    except CommandError as e:
        sys.stderr.write(f'{e}\n')
        sys.exit(1)
    except SystemExit:
        raise


if __name__ == '__main__':
    main(sys.argv[1:])
