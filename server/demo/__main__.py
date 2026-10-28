"""The demo seeders as `python -m demo`:

    python -m demo --database <url> --yes [--company DEMO] [--only a,b]            the seed_demo_suite command
    python -m demo group --database <url> --yes [--sister DEMOSYS] [--no-suite]     the seed_demo_group command
"""
import os
import sys

_DJANGO_OPTIONS = {'help', 'version', 'verbosity', 'settings', 'pythonpath', 'traceback', 'no_color', 'force_color', 'skip_checks'}


def main(argv: list[str]) -> None:
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
    import django
    django.setup()
    from django.core.management import call_command
    from django.core.management.base import CommandError

    if argv[:1] == ['group']:
        from demo.management.commands.seed_demo_group import Command
        name, argv = 'seed_demo_group', argv[1:]
    else:
        from demo.management.commands.seed_demo_suite import Command
        name = 'seed_demo_suite'
    cmd = Command()
    parser = cmd.create_parser('python -m demo', name)
    opts = vars(parser.parse_args(argv))
    own = {a.dest for a in parser._actions} - _DJANGO_OPTIONS
    try:
        call_command(cmd, **{k: v for k, v in opts.items() if k in own})
    except CommandError as e:
        sys.stderr.write(f'{e}\n')
        sys.exit(1)


if __name__ == '__main__':
    main(sys.argv[1:])
