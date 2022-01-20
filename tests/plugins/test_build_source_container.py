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
from atomic_reactor.plugin import BuildStepPluginsRunner, PluginFailedException
from atomic_reactor.plugins.build_source_container import SourceContainerPlugin
from atomic_reactor.utils import retries


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
def test_running_build(workflow, caplog,
                       sources_dir, sources_dir_exists, sources_dir_empty,
                       remote_dir, remote_dir_exists, remote_dir_empty,
                       maven_dir, maven_dir_exists, maven_dir_empty,
                       export_failed):
    """
    Test if proper result is returned and if plugin works
    """
    build_sources_dir = workflow.build_dir.source_container_sources_dir
    sources_dir_path = build_sources_dir / sources_dir
    if sources_dir_exists:
        sources_dir_path.mkdir()
        if not sources_dir_empty:
            os.mknod(sources_dir_path / 'stub.srpm')

    remote_dir_path = build_sources_dir / remote_dir
    if remote_dir_exists:
        remote_dir_path.mkdir()
        if not remote_dir_empty:
            os.mknod(remote_dir_path / 'remote-sources-first.tar.gz')
            os.mknod(remote_dir_path / 'remote-sources-second.tar.gz')

    maven_dir_path = build_sources_dir / maven_dir
    if maven_dir_exists:
        maven_dir_path.mkdir()
        if not maven_dir_empty:
            os.mkdir(maven_dir_path / 'maven-sources-1')
            os.mknod(maven_dir_path / 'maven-sources-1' / 'maven-sources-1.tar.gz')

    workflow.build_dir.init_build_dirs(["noarch"], workflow.source)
    workflow.data.prebuild_results[PLUGIN_FETCH_SOURCES_KEY] = {
        'image_sources_dir': str(sources_dir_path),
        'remote_sources_dir': str(remote_dir_path),
        'maven_sources_dir': str(maven_dir_path),
    }

    runner = BuildStepPluginsRunner(
        workflow,
        [{
            'name': SourceContainerPlugin.key,
            'args': {},
        }]
    )

    temp_image_output_dir = workflow.build_dir.source_container_output_dir
    exported_image_file = workflow.build_dir.any_platform.exported_squashed_image
    temp_image_export_dir = exported_image_file.parent
    tempfile_chain = (flexmock(tempfile)
                      .should_receive("mkdtemp")
                      .and_return(str(temp_image_output_dir)))
    tempfile_chain.and_return(str(temp_image_export_dir))
    temp_image_export_dir.mkdir(parents=True, exist_ok=True)
    temp_image_output_dir.joinpath('blobs', 'sha256').mkdir(parents=True, exist_ok=True)
    # temp dir created by bsi
    flexmock(os).should_receive('getcwd').and_return(str(workflow.build_dir.path))
    temp_bsi_dir = workflow.build_dir.path / 'SrcImg'
    temp_bsi_dir.mkdir()

    def check_run_skopeo(args):
        """Mocked call to skopeo"""
        assert args[0] == 'skopeo'
        assert args[1] == 'copy'
        assert args[2] == 'oci:%s' % temp_image_output_dir
        assert args[3] == f'docker-archive:{exported_image_file}'

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
            args_expect.append(str(sources_dir_path))
        if remote_dir and remote_dir_exists:
            for count in range(len(os.listdir(remote_dir_path))):
                args_expect.append('-e')
                args_expect.append(str(remote_dir_path / f"remote_source_{count}"))
        if maven_dir and maven_dir_exists:
            for maven_subdir in os.listdir(maven_dir_path):
                args_expect.append('-e')
                args_expect.append(str(maven_dir_path / maven_subdir))
        args_expect.append('-o')
        args_expect.append(str(temp_image_output_dir))

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

    temp_image_output_dir.joinpath("index.json").write_text(json.dumps(index_json), "utf-8")
    temp_image_output_dir.joinpath("blobs", "sha256", blob_sha).write_text(
        json.dumps(blob_json), "utf-8"
    )

    if not export_failed:
        export_tar = workflow.build_dir.any_platform.exported_squashed_image
        with open(export_tar, "wb") as f:
            with tarfile.TarFile(mode="w", fileobj=f) as tf:
                for f in os.listdir(temp_image_output_dir):
                    tf.add(str(temp_image_output_dir / f), f)

    if not any([sources_dir_exists, remote_dir_exists, maven_dir_exists]):
        build_result = runner.run()
        err_msg = f"No SRPMs directory '{sources_dir_path}' available"
        err_msg += f"\nNo Remote source directory '{remote_dir_path}' available"
        err_msg += f"\nNo Maven source directory '{maven_dir_path}' available"
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
        empty_srpm_msg = f"SRPMs directory '{sources_dir_path}' is empty"
        empty_remote_msg = f"Remote source directory '{remote_dir_path}' is empty"
        empty_maven_msg = f"Maven source directory '{maven_dir_path}' is empty"
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


def test_failed_build(workflow, source_dir, caplog, user_params):
    """
    Test if proper error state is returned when build inside build
    container failed
    """
    (flexmock(subprocess).should_receive('check_output')
     .and_raise(subprocess.CalledProcessError(1, 'cmd', output='stub stdout')))
    some_dir = workflow.build_dir.path / 'some_dir'
    some_dir.mkdir()
    workflow.data.prebuild_results[PLUGIN_FETCH_SOURCES_KEY] = {
        'image_sources_dir': str(some_dir),
        'remote_sources_dir': str(some_dir),
        'maven_sources_dir': str(some_dir),
    }
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
