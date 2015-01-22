import os
import shutil
from dock.core import DockerTasker
from dock.inner import DockerBuildWorkflow
from dock.plugin import PreBuildPluginsRunner, PostBuildPluginsRunner
from dock.plugins.pre_inject_yum_repo import InjectYumRepoPlugin


git_url = "https://github.com/TomasTomecek/docker-hello-world.git"
TEST_IMAGE = "fedora:latest"


class X(object):
    pass


def test_yuminject_plugin(tmpdir):
    this_dir = os.path.dirname(os.path.abspath(__file__))
    tmp_df = os.path.join(str(tmpdir), 'Dockerfile')
    shutil.copy2(os.path.join(this_dir, 'Dockerfile'), tmp_df)

    tasker = DockerTasker()
    workflow = DockerBuildWorkflow(git_url, "test-image")
    setattr(workflow, 'builder', X)

    metalink = 'https://mirrors.fedoraproject.org/metalink?repo=fedora-\$releasever&arch=\$basearch'

    workflow.repos['yum'] = [{
        'name': 'my-repo',
        'metalink': metalink,
        'enabled': 1,
        'gpgcheck': 0,
    }]
    setattr(workflow.builder, 'image_id', "asd123")
    setattr(workflow.builder, 'df_path', tmp_df)
    setattr(workflow.builder, 'base_image_name', "fedora")
    setattr(workflow.builder, 'base_tag', "21")
    runner = PreBuildPluginsRunner(tasker, workflow,
                                   [{
                                       'name': InjectYumRepoPlugin.key,
                                       'args': {}}])
    runner.run()
    assert InjectYumRepoPlugin.key is not None
    with open(tmp_df, 'r') as fd:
        altered_df = fd.read()
    assert metalink in altered_df
