#!/usr/bin/env python3
"""Check images used by existing Compose apps without pulling or changing containers."""
import argparse
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from orca_app.image_updates import Docker, app_identity, check_apps, registry_image_id, repository

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--context', help='Docker context to inspect (default: current Docker context).')
    parser.add_argument('--app', help='Check only this Compose project or standalone container name.')
    parser.add_argument('--json', action='store_true', help='Print the complete results as JSON.')
    args = parser.parse_args()
    try:
        result = check_apps(Docker(args.context), args.app)
    except RuntimeError as error:
        print(f'Unable to check apps: {error}', file=sys.stderr)
        return 2
    if args.app and not result['apps']:
        print(f'No app found with name {args.app!r}.', file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(result['scope'])
        if not result['apps']:
            print('No apps found.')
        labels = dict(update='UPDATE AVAILABLE', unknown='UNABLE TO CHECK FULLY',
                      pinned='PINNED — NO UPDATE TAG', partial='PARTIALLY CHECKED',
                      current='IMAGES UP TO DATE')
        for app in result['apps']:
            updates = sorted({r['service'] for r in app['containers'] if r['status'] == 'update'})
            detail = ' — ' + ', '.join(updates) if updates else ''
            print(f"{app['name']} [{app['kind']}]: {labels[app['status']]}{detail}")
            # Preserve failures even if another service has a known update.
            for record in app['containers']:
                if record.get('detail'):
                    print(f"  {record['service']}: {record['detail']}")
        for warning in result['warnings']:
            print('Warning: ' + warning)
    return 2 if result['warnings'] or any(
        r['status'] == 'unknown' for app in result['apps'] for r in app['containers']) else 0


if __name__ == '__main__':
    sys.exit(main())
