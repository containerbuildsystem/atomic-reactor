from dock.core import DockerTasker
from dock.inner import DockerBuildWorkflow
from dock.plugin import PreBuildPluginsRunner, PreBuildPlugin
from dock.plugins.pre_add_yum_repo_by_url import AddYumRepoByUrlPlugin
from dock.util import ImageName


git_url = "https://github.com/TomasTomecek/docker-hello-world.git"
TEST_IMAGE = "fedora:latest"


class MockResponse(object):
    def raise_for_status(self):
        pass

@staticmethod
def fake_get(url):
    # Mock requests.get
    response = MockResponse()
    if url.endswith("2repos.repo"):
        text = """
[test1]
name=Test 1 $releasever - $basearch (ignored)
baseurl=http://example.com/xyzzy/repo1/$releasever/
enabled=1
gpgcheck=1

[test2]
name=Test 2 $releasever - $basearch (ignored)
baseurl=http://example.com/xyzzy/repo2/$releasever/
#metadata_expire=7d (ignored)
enabled=1
gpgcheck=1
"""
    else:
        text = """
[test3]
name=Test 3 $releasever - $basearch (ignored)
metalink=https://mirrors.fedoraproject.org/metalink?repo=fedora-$releasever&arch=$basearch
enabled=1
gpgcheck=1
gpgkey=file:///etc/pki/rpm-gpg/RPM-GPG-KEY-fedora-$releasever-$basearch
"""

    try:
        # py2
        response.text = unicode(text)
    except:
        # py3
        response.text = text

    return response


class X(object):
    pass


def prepare(tmpdir):
    tasker = DockerTasker()
    workflow = DockerBuildWorkflow(git_url, "test-image")
    setattr(workflow, 'builder', X)

    workflow.repos['yum'] = []

    setattr(workflow.builder, 'image_id', "asd123")
    setattr(workflow.builder, 'df_path', "/tmp/nonexistent")
    setattr(workflow.builder, 'base_image', ImageName(repo='Fedora', tag='21'))
    setattr(workflow.builder, 'git_dockerfile_path', None)
    setattr(workflow.builder, 'git_path', None)
    return tasker, workflow

def test_no_repourls(tmpdir):
    tasker, workflow = prepare(tmpdir)
    runner = PreBuildPluginsRunner(tasker, workflow, [{
        'name': AddYumRepoByUrlPlugin.key,
        'args': { 'repourls': [] }}])
    runner.run()
    assert AddYumRepoByUrlPlugin.key is not None
    assert len (workflow.repos['yum']) == 0

def test_single_repourl(tmpdir):
    tasker, workflow = prepare(tmpdir)
    runner = PreBuildPluginsRunner(tasker, workflow, [{
        'name': AddYumRepoByUrlPlugin.key,
        'args': { 'repourls': ['http://example.com/2repos.repo'] }}])
    runner.plugin_classes[AddYumRepoByUrlPlugin.key]._get = fake_get
    runner.run()

    assert len (workflow.repos['yum']) == 2

    expected1 = { 'name': r'test1',
                  'baseurl': r'http://example.com/xyzzy/repo1/$releasever/',
                  'enabled': r'1',
                  'gpgcheck': r'1' }
    assert expected1 in workflow.repos['yum']

    expected2 = { 'name': r'test2',
                  'baseurl': r'http://example.com/xyzzy/repo2/$releasever/',
                  'enabled': r'1',
                  'gpgcheck': r'1' }
    assert expected2 in workflow.repos['yum']

def test_multiple_repourls(tmpdir):
    tasker, workflow = prepare(tmpdir)
    runner = PreBuildPluginsRunner(tasker, workflow, [{
        'name': AddYumRepoByUrlPlugin.key,
        'args': { 'repourls': ['http://example.com/2repos.repo',
                               'http://example.com/xyzzy'] }}])
    runner.plugin_classes[AddYumRepoByUrlPlugin.key]._get = fake_get
    runner.run()

    assert len (workflow.repos['yum']) == 3

    expected1 = { 'name': r'test1',
                  'baseurl': r'http://example.com/xyzzy/repo1/$releasever/',
                  'enabled': r'1',
                  'gpgcheck': r'1' }
    assert expected1 in workflow.repos['yum']

    expected2 = { 'name': r'test2',
                  'baseurl': r'http://example.com/xyzzy/repo2/$releasever/',
                  'enabled': r'1',
                  'gpgcheck': r'1' }
    assert expected2 in workflow.repos['yum']

    expected3 = { 'name': r'test3',
                  'metalink': r'https://mirrors.fedoraproject.org/metalink?repo=fedora-$releasever&arch=$basearch',
                  'enabled': r'1',
                  'gpgcheck': r'1',
                  'gpgkey': r'file:///etc/pki/rpm-gpg/RPM-GPG-KEY-fedora-$releasever-$basearch' }
    assert expected3 in workflow.repos['yum']
