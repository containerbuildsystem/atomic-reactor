"""
Copyright (c) 2019 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""
import os
import subprocess
import tempfile

from flexmock import flexmock
import pytest
import json
import tarfile
import re

from atomic_reactor.constants import PLUGIN_FETCH_SOURCES_KEY
from atomic_reactor.inner import DockerBuildWorkflow
from atomic_reactor.constants import EXPORTED_SQUASHED_IMAGE_NAME
from atomic_reactor.plugin import BuildStepPluginsRunner, PluginFailedException
from atomic_reactor.plugins.build_source_container import SourceContainerPlugin
from atomic_reactor.utils import retries


class MockSource(object):

    def __init__(self, tmpdir):
        tmpdir = str(tmpdir)
        self.dockerfile_path = os.path.join(tmpdir, 'Dockerfile')
        self.path = tmpdir
        self.config = flexmock(image_build_method=None)
        self.workdir = tmpdir

    def get_build_file_path(self):
        return self.dockerfile_path, self.path


def mock_workflow(tmpdir, sources_dir='', remote_dir='', maven_dir=''):
    workflow = DockerBuildWorkflow(source=None)
    source = MockSource(tmpdir)
    setattr(workflow, 'source', source)

    workflow.prebuild_results[PLUGIN_FETCH_SOURCES_KEY] = {
        'image_sources_dir': os.path.join(tmpdir.strpath, sources_dir),
        'remote_sources_dir': os.path.join(tmpdir.strpath, remote_dir),
        'maven_sources_dir': os.path.join(tmpdir.strpath, maven_dir),
    }

    return workflow


@pytest.mark.parametrize('sources_dir, sources_dir_exists, sources_dir_empty', [
    ('sources_dir', False, True),
    ('sources_dir', True, True),
    ('sources_dir', True, False)])
@pytest.mark.parametrize('remote_dir, remote_dir_exists, remote_dir_empty', [
    ('remote_sources_dir', False, True),
    ('remote_sources_dir', True, True),
    ('remote_sources_dir', True, False)])
@pytest.mark.parametrize('maven_dir, maven_dir_exists, maven_dir_empty', [
    ('maven_sources_dir', False, True),
    ('maven_sources_dir', True, True),
    ('maven_sources_dir', True, False)])
@pytest.mark.parametrize('export_failed', (True, False))
def test_running_build(tmpdir, caplog, user_params,
                       sources_dir, sources_dir_exists, sources_dir_empty,
                       remote_dir, remote_dir_exists, remote_dir_empty,
                       maven_dir, maven_dir_exists, maven_dir_empty,
                       export_failed):
    """
    Test if proper result is returned and if plugin works
    """
    sources_dir_path = os.path.join(tmpdir.strpath, sources_dir)
    if sources_dir_exists:
        os.mkdir(sources_dir_path)
        if not sources_dir_empty:
            os.mknod(os.path.join(sources_dir_path, 'stub.srpm'))

    remote_dir_path = os.path.join(tmpdir.strpath, remote_dir)
    if remote_dir_exists:
        os.mkdir(remote_dir_path)
        if not remote_dir_empty:
            os.mknod(os.path.join(remote_dir_path, 'remote-sources-first.tar.gz'))
            os.mknod(os.path.join(remote_dir_path, 'remote-sources-second.tar.gz'))

    maven_dir_path = os.path.join(tmpdir.strpath, maven_dir)
    if maven_dir_exists:
        os.mkdir(maven_dir_path)
        if not maven_dir_empty:
            os.mkdir(os.path.join(maven_dir_path, 'maven-sources-1'))
            os.mknod(os.path.join(maven_dir_path, 'maven-sources-1', 'maven-sources-1.tar.gz'))

    workflow = mock_workflow(tmpdir, sources_dir, remote_dir, maven_dir)

    runner = BuildStepPluginsRunner(
        workflow,
        [{
            'name': SourceContainerPlugin.key,
            'args': {},
        }]
    )

    temp_image_output_dir = os.path.join(str(tmpdir), 'output')
    temp_image_export_dir = str(tmpdir)
    tempfile_chain = flexmock(tempfile).should_receive("mkdtemp").and_return(temp_image_output_dir)
    tempfile_chain.and_return(temp_image_export_dir)
    os.makedirs(temp_image_export_dir, exist_ok=True)
    os.makedirs(os.path.join(temp_image_output_dir, 'blobs', 'sha256'))
    # temp dir created by bsi
    flexmock(os).should_receive('getcwd').and_return(str(tmpdir))
    temp_bsi_dir = os.path.join(str(tmpdir), 'SrcImg')
    os.mkdir(temp_bsi_dir)

    def check_run_skopeo(args):
        """Mocked call to skopeo"""
        assert args[0] == 'skopeo'
        assert args[1] == 'copy'
        assert args[2] == 'oci:%s' % temp_image_output_dir
        assert args[3] == 'docker-archive:%s' % os.path.join(temp_image_export_dir,
                                                             EXPORTED_SQUASHED_IMAGE_NAME)

        if export_failed:
            raise subprocess.CalledProcessError(returncode=1, cmd=args, output="Failed")

        return ''

    def check_check_output(args, **kwargs):
        """Mocked check_output call for bsi"""
        args_expect = ['bsi', '-d']
        drivers = set()
        if sources_dir and sources_dir_exists:
            drivers.add('sourcedriver_rpm_dir')
        if remote_dir and remote_dir_exists:
            drivers.add('sourcedriver_extra_src_dir')
        if maven_dir and maven_dir_exists:
            drivers.add('sourcedriver_extra_src_dir')
        args_expect.append(','.join(drivers))

        if sources_dir and sources_dir_exists:
            args_expect.append('-s')
            args_expect.append(sources_dir_path)
        if remote_dir and remote_dir_exists:
            for count in range(len(os.listdir(os.path.join(tmpdir, remote_dir)))):
                args_expect.append('-e')
                args_expect.append(os.path.join(remote_dir_path, f"remote_source_{count}"))
        if maven_dir and maven_dir_exists:
            for maven_subdir in os.listdir(os.path.join(tmpdir, maven_dir)):
                args_expect.append('-e')
                args_expect.append(os.path.join(tmpdir, maven_dir, maven_subdir))
        args_expect.append('-o')
        args_expect.append(temp_image_output_dir)

        assert args == args_expect
        return 'stub stdout'

    any_sources = any([sources_dir_exists, remote_dir_exists, maven_dir_exists])

    (flexmock(retries)
     .should_receive("run_cmd")
     .times(1 if any_sources else 0)
     .replace_with(check_run_skopeo))

    (flexmock(subprocess)
     .should_receive("check_output")
     .times(1 if any_sources else 0)
     .replace_with(check_check_output))

    blob_sha = "f568c411849e21aa3917973f1c5b120f6b52fe69b1944dfb977bc11bed6fbb6d"
    index_json = {"schemaVersion": 2,
                  "manifests":
                      [{"mediaType": "application/vnd.oci.image.manifest.v1+json",
                        "digest": "sha256:%s" % blob_sha,
                        "size": 645,
                        "annotations": {"org.opencontainers.image.ref.name": "latest-source"},
                        "platform": {"architecture": "amd64", "os": "linux"}}]}
    blob_json = {"schemaVersion": 2, "layers": []}

    with open(os.path.join(temp_image_output_dir, 'index.json'), 'w') as fp:
        fp.write(json.dumps(index_json))
    with open(os.path.join(temp_image_output_dir, 'blobs', 'sha256', blob_sha), 'w') as fp:
        fp.write(json.dumps(blob_json))

    if not export_failed:
        export_tar = os.path.join(temp_image_export_dir, EXPORTED_SQUASHED_IMAGE_NAME)
        with open(export_tar, "wb") as f:
            with tarfile.TarFile(mode="w", fileobj=f) as tf:
                for f in os.listdir(temp_image_output_dir):
                    tf.add(os.path.join(temp_image_output_dir, f), f)

    if not any([sources_dir_exists, remote_dir_exists, maven_dir_exists]):
        build_result = runner.run()
        err_msg = "No SRPMs directory '{}' available".format(sources_dir_path)
        err_msg += "\nNo Remote source directory '{}' available".format(remote_dir_path)
        err_msg += "\nNo Maven source directory '{}' available".format(maven_dir_path)
        # Since Python 3.7 logger adds additional whitespaces by default -> checking without them
        assert re.sub(r'\s+', " ", err_msg) in re.sub(r'\s+', " ", caplog.text)
        assert build_result.is_failed()

    elif export_failed:
        with pytest.raises(PluginFailedException):
            runner.run()
    else:
        build_result = runner.run()
        assert not build_result.is_failed()
        assert build_result.source_docker_archive
        assert 'stub stdout' in caplog.text
        empty_srpm_msg = "SRPMs directory '{}' is empty".format(sources_dir_path)
        empty_remote_msg = "Remote source directory '{}' is empty".format(remote_dir_path)
        empty_maven_msg = "Maven source directory '{}' is empty".format(maven_dir_path)
        if sources_dir_exists and sources_dir_empty:
            assert empty_srpm_msg in caplog.text
        else:
            assert empty_srpm_msg not in caplog.text
        if remote_dir_exists and remote_dir_empty:
            assert empty_remote_msg in caplog.text
        else:
            assert empty_remote_msg not in caplog.text
        if maven_dir_exists and maven_dir_empty:
            assert empty_maven_msg in caplog.text
        else:
            assert empty_maven_msg not in caplog.text

        remove_srpm_msg = f"Will remove directory with downloaded srpms: {sources_dir_path}"
        remove_remote_msg = f"Will remove directory with downloaded remote sources: " \
                            f"{remote_dir_path}"
        remove_maven_msg = f"Will remove directory with downloaded maven sources: " \
                           f"{maven_dir_path}"
        if sources_dir_exists:
            assert remove_srpm_msg in caplog.text
        else:
            assert remove_srpm_msg not in caplog.text
        if remote_dir_exists:
            assert remove_remote_msg in caplog.text
        else:
            assert remove_remote_msg not in caplog.text
        if maven_dir_exists:
            assert remove_maven_msg in caplog.text
        else:
            assert remove_maven_msg not in caplog.text

        remove_unpacked_msg = f"Will remove unpacked image directory: {temp_image_output_dir}"
        assert remove_unpacked_msg in caplog.text

        remove_tmpbsi_msg = f"Will remove BSI temporary directory: {temp_bsi_dir}"
        assert remove_tmpbsi_msg in caplog.text


def test_failed_build(tmpdir, caplog, user_params):
    """
    Test if proper error state is returned when build inside build
    container failed
    """
    (flexmock(subprocess).should_receive('check_output')
     .and_raise(subprocess.CalledProcessError(1, 'cmd', output='stub stdout')))
    workflow = mock_workflow(tmpdir)
    runner = BuildStepPluginsRunner(
        workflow,
        [{
            'name': SourceContainerPlugin.key,
            'args': {},
        }]
    )

    build_result = runner.run()
    assert build_result.is_failed()
    assert 'BSI failed with output:' in caplog.text
    assert 'stub stdout' in caplog.text
