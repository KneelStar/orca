"""Read-only app discovery, registry comparison, and incomplete-result handling."""
import unittest

from scripts.check_app_updates import check_apps, registry_image_id, repository


def digest(character):
    return 'sha256:' + character * 64


def container(name, image, current, project='media', service='web', extra=None):
    labels = {'com.docker.compose.project': project, 'com.docker.compose.service': service} if project else {}
    labels.update(extra or {})
    return dict(Name='/' + name, Image=current, State=dict(Running=True),
                Config=dict(Image=image, Labels=labels))


class FakeDocker:
    def __init__(self, containers, targets):
        self.containers = containers
        self.targets = targets
        self.calls = []
        self.descriptors = {}
        self.image_metadata = dict(Os='linux', Architecture='amd64')

    def text(self, *args):
        self.calls.append(args)
        if args == ('ps', '--all', '--quiet'):
            return '\n'.join(self.containers)
        raise AssertionError('Unexpected or mutating Docker command: ' + repr(args))

    def json(self, *args):
        self.calls.append(args)
        if args[:2] == ('container', 'inspect'):
            return [self.containers[args[2]]]
        if args[:2] == ('image', 'inspect'):
            return [self.image_metadata]
        if args[:3] == ('buildx', 'imagetools', 'inspect'):
            if args[3] == '--format':
                if args[4] == '{{json .Manifest}}':
                    return dict(digest=self.descriptors[args[-1]])
                return dict(os='linux', architecture='amd64')
            target = self.targets[args[-1]]
            if isinstance(target, Exception):
                raise target
            return target if isinstance(target, dict) else dict(config=dict(digest=target))
        raise AssertionError('Unexpected or mutating Docker command: ' + repr(args))


class AppUpdateTests(unittest.TestCase):
    def test_containerd_compares_platform_manifest_instead_of_index_or_config(self):
        reference = 'portainer/portainer-ce:lts'
        row = container('portainer', reference, digest('a'))
        row['ImageManifestDescriptor'] = dict(digest=digest('d'), platform=dict(os='linux', architecture='amd64'))
        docker = FakeDocker({'a': row}, {
            'portainer/portainer-ce@' + digest('e'): dict(manifests=[
                dict(digest=digest('d'), platform=dict(os='linux', architecture='amd64')),
                dict(digest=digest('f'), platform=dict(os='linux', architecture='arm64')),
            ]),
            'portainer/portainer-ce@' + digest('d'): digest('b'),
        })
        docker.descriptors[reference] = digest('e')
        # Image-index inspection can default to a different host platform.
        docker.image_metadata['Architecture'] = 'arm64'
        # The index changed for another architecture; our platform is unchanged.
        self.assertEqual(check_apps(docker)['apps'][0]['status'], 'current')
        row['ImageManifestDescriptor']['digest'] = digest('c')
        self.assertEqual(check_apps(docker)['apps'][0]['status'], 'update')

    def test_containerd_single_platform_manifest(self):
        row = container('web', 'web:stable', digest('d'))
        row['ImageManifestDescriptor'] = dict(digest=digest('d'))
        docker = FakeDocker({'a': row}, {'web@' + digest('d'): digest('b')})
        docker.descriptors['web:stable'] = digest('d')
        self.assertEqual(check_apps(docker)['apps'][0]['status'], 'current')

    def test_missing_containerd_manifest_metadata_reports_unknown(self):
        docker = FakeDocker({'a': container('web', 'web:stable', digest('a'))}, {})
        docker.image_metadata['Descriptor'] = dict(digest=digest('a'))
        result = check_apps(docker)['apps'][0]
        self.assertEqual(result['status'], 'unknown')
        self.assertIn('manifest digest', result['containers'][0]['detail'])

    def test_groups_services_and_replicas_and_checks_shared_images_once(self):
        docker = FakeDocker({
            'a': container('web-1', 'web:stable', digest('a')),
            'b': container('web-2', 'web:stable', digest('b')),
            'c': container('db-1', 'db:16', digest('c'), service='db'),
            'd': container('other-web', 'web:stable', digest('a'), project='other'),
        }, {'web:stable': digest('b'), 'db:16': digest('c')})
        result = check_apps(docker)
        self.assertEqual(len(result['apps']), 2)
        media = result['apps'][0]
        self.assertEqual(media['status'], 'update')
        self.assertEqual([r['status'] for r in media['containers']], ['update', 'current', 'current'])
        self.assertEqual(docker.calls.count(('buildx', 'imagetools', 'inspect', '--raw', 'web:stable')), 1)

    def test_current_pinned_unavailable_and_standalone_are_distinct(self):
        docker = FakeDocker({
            'a': container('current', 'current:stable', digest('a'), project='current'),
            'b': container('pinned', 'web@' + digest('b'), digest('b'), project='pinned'),
            'c': container('private', 'private:stable', digest('c'), project='private'),
            'd': container('solo', digest('d'), digest('d'), project=None),
        }, {'current:stable': digest('a'), 'private:stable': RuntimeError('Authentication required.')})
        apps = {app['name']: app for app in check_apps(docker)['apps']}
        self.assertEqual(apps['current']['status'], 'current')
        self.assertEqual(apps['pinned']['status'], 'pinned')
        self.assertEqual(apps['private']['status'], 'unknown')
        self.assertEqual(apps['solo']['kind'], 'container')
        self.assertEqual(apps['solo']['status'], 'unknown')

    def test_known_update_keeps_other_service_errors_visible(self):
        docker = FakeDocker({
            'a': container('web', 'web:stable', digest('a')),
            'b': container('db', 'db:stable', digest('c'), service='db'),
        }, {'web:stable': digest('b'), 'db:stable': RuntimeError('Registry offline.')})
        app = check_apps(docker)['apps'][0]
        self.assertEqual(app['status'], 'update')
        self.assertEqual(app['containers'][1]['detail'], 'Registry offline.')

    def test_oneoffs_and_local_swarm_tasks_are_checked_without_claiming_full_stack_discovery(self):
        docker = FakeDocker({
            'a': container('migration', 'web:stable', digest('a'), extra={'com.docker.compose.oneoff': 'True'}),
            'b': container('task', 'web:stable', digest('a'), project=None,
                           extra={'com.docker.swarm.service.name': 'stack_web'}),
        }, {'web:stable': digest('a')})
        apps = check_apps(docker)['apps']
        self.assertEqual(len(apps), 2)
        self.assertEqual(apps[1]['kind'], 'swarm-service')
        self.assertTrue(all(app['status'] == 'current' for app in apps))

    def test_app_filter_includes_stopped_containers(self):
        stopped = container('stopped', 'web:stable', digest('a'))
        stopped['State']['Running'] = False
        docker = FakeDocker({
            'a': container('web', 'web:stable', digest('a')),
            'b': container('other', 'other:stable', digest('b'), project='other'),
            'c': stopped,
        }, {'web:stable': digest('a')})
        result = check_apps(docker, 'media')
        self.assertEqual([app['name'] for app in result['apps']], ['media'])
        self.assertEqual(len(result['warnings']), 0)
        self.assertEqual(len(result['apps'][0]['containers']), 2)

    def test_selects_platform_manifest_ignores_attestations_and_registry_port(self):
        target = digest('b')
        reference = 'registry.example:5000/team/web:stable'
        child = 'registry.example:5000/team/web@' + digest('d')
        docker = FakeDocker({}, {
            reference: dict(manifests=[
                dict(digest=digest('a'), platform=dict(os='linux', architecture='arm64')),
                dict(digest=digest('d'), platform=dict(os='linux', architecture='amd64')),
                dict(digest=digest('c'), platform=dict(os='unknown', architecture='unknown')),
            ]), child: target,
        })
        self.assertEqual(registry_image_id(docker, reference, ('linux', 'amd64', '', '')), target)
        self.assertIn(('buildx', 'imagetools', 'inspect', '--raw', child), docker.calls)
        self.assertEqual(repository('ubuntu:stable'), 'ubuntu')

    def test_missing_ambiguous_or_incompatible_platform_is_unknown(self):
        reference = 'web:stable'
        docker = FakeDocker({}, {reference: dict(manifests=[
            dict(digest=digest('a'), platform=dict(os='linux', architecture='arm64', variant='v8')),
            dict(digest=digest('b'), platform=dict(os='linux', architecture='arm64', variant='v9')),
        ])})
        for platform in [('linux', 'amd64', '', ''), ('linux', 'arm64', '', '')]:
            with self.assertRaisesRegex(RuntimeError, 'missing or ambiguous'):
                registry_image_id(docker, reference, platform)
        docker = FakeDocker({}, {reference: digest('a')})
        with self.assertRaisesRegex(RuntimeError, 'does not match'):
            registry_image_id(docker, reference, ('linux', 'arm64', '', ''))


if __name__ == '__main__':
    unittest.main()
