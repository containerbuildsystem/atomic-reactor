"""
Copyright (c) 2017 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from flexmock import flexmock
import os
import pytest
import subprocess
import tarfile

from modulemd import ModuleMetadata

from atomic_reactor.constants import IMAGE_TYPE_OCI, IMAGE_TYPE_OCI_TAR
from atomic_reactor.inner import DockerBuildWorkflow
from atomic_reactor.plugin import PrePublishPluginsRunner
from atomic_reactor.plugins.prepub_flatpak_create_oci import FlatpakCreateOciPlugin
from atomic_reactor.plugins.pre_flatpak_create_dockerfile import (FlatpakSourceInfo,
                                                                  set_flatpak_source_info)
from atomic_reactor.util import ImageName

from tests.constants import TEST_IMAGE
from tests.fixtures import docker_tasker  # noqa
from tests.flatpak import (
    FLATPAK_APP_JSON, FLATPAK_APP_MODULEMD,
    FLATPAK_RUNTIME_JSON, FLATPAK_RUNTIME_MODULEMD
)

CONTAINER_ID = 'CONTAINER-ID'

ROOT = '/var/tmp/flatpak-build'

DESKTOP_FILE_CONTENTS = """[Desktop Entry]
Name=Image Viewer
Comment=Browse and rotate images
TryExec=eog
Exec=eog %U
Icon=eog
StartupNotify=true
Terminal=false
Type=Application
Categories=GNOME;GTK;Graphics;2DGraphics;RasterGraphics;Viewer;
MimeType=image/bmp;image/gif;image/jpeg;image/jpg;image/pjpeg;image/png;image/tiff;image/x-bmp;image/x-gray;image/x-icb;image/x-ico;image/x-png;image/x-portable-anymap;image/x-portable-bitmap;image/x-portable-graymap;image/x-portable-pixmap;image/x-xbitmap;image/x-xpixmap;image/x-pcx;image/svg+xml;image/svg+xml-compressed;image/vnd.wap.wbmp;
# Extra keywords that can be used to search for eog in GNOME Shell and Unity
Keywords=Picture;Slideshow;Graphics;"""

APP_FILESYSTEM_CONTENTS = {
    '/usr/bin/not_eog': 'SHOULD_IGNORE',
    ROOT + '/usr/bin/also_not_eog': 'SHOULD_IGNORE',
    ROOT + '/app/bin/eog': 'MY_PROGRAM',
    ROOT + '/app/share/applications/eog.desktop': DESKTOP_FILE_CONTENTS,
    ROOT + '/app/share/icons/hicolor/256x256/apps/eog.png': 'MY_ICON',
}

EXPECTED_APP_FLATPAK_CONTENTS = [
    '/export/share/applications/org.gnome.eog.desktop',
    '/export/share/icons/hicolor/256x256/apps/org.gnome.eog.png',
    '/files/bin/eog',
    '/files/share/applications/org.gnome.eog.desktop',
    '/files/share/icons/hicolor/256x256/apps/eog.png',
    '/files/share/icons/hicolor/256x256/apps/org.gnome.eog.png',
    '/metadata'
]

APP_CONFIG = {
    'module_name': 'eog',
    'module_stream': 'f26',
    'module_version': '20170629213428',
    'flatpak_json': FLATPAK_APP_JSON,
    'module_metadata': FLATPAK_APP_MODULEMD,
    'filesystem_contents': APP_FILESYSTEM_CONTENTS,
    'expected_contents': EXPECTED_APP_FLATPAK_CONTENTS
}

RUNTIME_FILESYSTEM_CONTENTS = {
    '/usr/bin/not_eog': 'SHOULD_IGNORE',
    ROOT + '/etc/passwd': 'SOME_CONFIG_FILE',
    ROOT + '/usr/bin/bash': 'SOME_BINARY',
    ROOT + '/usr/lib64/libfoo.so.1.0.0': 'SOME_LIB',
}

EXPECTED_RUNTIME_FLATPAK_CONTENTS = [
    '/files/bin/bash',
    '/files/etc/passwd',
    '/files/lib64/libfoo.so.1.0.0',
    '/metadata'
]

RUNTIME_CONFIG = {
    'module_name': 'flatpak-runtime',
    'module_stream': 'f26',
    'module_version': '20170629185228',
    'flatpak_json': FLATPAK_RUNTIME_JSON,
    'module_metadata': FLATPAK_RUNTIME_MODULEMD,
    'filesystem_contents': RUNTIME_FILESYSTEM_CONTENTS,
    'expected_contents': EXPECTED_RUNTIME_FLATPAK_CONTENTS
}

CONFIGS = {
    'app': APP_CONFIG,
    'runtime': RUNTIME_CONFIG
}


class MockSource(object):
    dockerfile_path = None
    path = None


class X(object):
    image_id = "xxx"
    source = MockSource()
    base_image = ImageName(repo="qwe", tag="asd")


@pytest.mark.parametrize('config_name', ('app', 'runtime'))  # noqa - docker_tasker fixture
def test_flatpak_create_oci(tmpdir, docker_tasker, config_name):
    config = CONFIGS[config_name]

    workflow = DockerBuildWorkflow({"provider": "git", "uri": "asd"}, TEST_IMAGE)
    setattr(workflow, 'builder', X)
    setattr(workflow.builder, 'tasker', docker_tasker)

    filesystem_dir = os.path.join(str(tmpdir), 'filesystem')
    os.mkdir(filesystem_dir)

    filesystem_contents = config['filesystem_contents']

    for path, contents in filesystem_contents.items():
        fullpath = os.path.join(filesystem_dir, path[1:])
        parent_dir = os.path.dirname(fullpath)
        os.makedirs(parent_dir)

        with open(fullpath, 'w') as f:
            f.write(contents)

    filesystem_tar = os.path.join(filesystem_dir, 'tar')
    with open(filesystem_tar, "wb") as f:
        with tarfile.TarFile(fileobj=f, mode='w') as tf:
            for f in os.listdir(filesystem_dir):
                tf.add(os.path.join(filesystem_dir, f), f)

    export_stream = open(filesystem_tar, "rb")

    (flexmock(docker_tasker.d.wrapped)
     .should_receive('create_container')
     .with_args(workflow.image)
     .and_return({'Id': CONTAINER_ID}))
    (flexmock(docker_tasker.d.wrapped)
     .should_receive('export')
     .with_args(CONTAINER_ID)
     .and_return(export_stream))
    (flexmock(docker_tasker.d.wrapped)
     .should_receive('remove_container')
     .with_args(CONTAINER_ID))

    mmd = ModuleMetadata()
    mmd.loads(config['module_metadata'])

    source = FlatpakSourceInfo(flatpak_json=FLATPAK_APP_JSON,
                               module_name=config['module_name'],
                               module_stream=config['module_stream'],
                               module_version=config['module_version'],
                               mmd=mmd)
    set_flatpak_source_info(workflow, source)

    runner = PrePublishPluginsRunner(
        docker_tasker,
        workflow,
        [{
            'name': FlatpakCreateOciPlugin.key,
            'args': {}
        }]
    )

    runner.run()

    dir_metadata = workflow.exported_image_sequence[-2]
    assert dir_metadata['type'] == IMAGE_TYPE_OCI

    tar_metadata = workflow.exported_image_sequence[-1]
    assert tar_metadata['type'] == IMAGE_TYPE_OCI_TAR

    # Import the OCI bundle into a ostree repository for examination
    repodir = os.path.join(str(tmpdir), 'repo')
    subprocess.check_call(['ostree', 'init', '--mode=archive-z2', '--repo=' + repodir])
    subprocess.check_call(['flatpak', 'build-import-bundle', '--oci',
                           repodir, dir_metadata['path']])

    ref_name = dir_metadata['ref_name']

    # Check that the expected files ended up in the flatpak
    output = subprocess.check_output(['ostree', '--repo=' + repodir,
                                      'ls', '-R', ref_name],
                                     universal_newlines=True)
    files = []
    for line in output.split('\n'):
        line = line.strip()
        if line == '':
            continue
        perms, user, group, size, path = line.split()
        if perms.startswith('d'):  # A directory
            continue
        files.append(path)

    assert sorted(files) == config['expected_contents']

    if config_name is 'app':
        # Check that the desktop file was rewritten
        output = subprocess.check_output(['ostree', '--repo=' + repodir,
                                          'cat', ref_name,
                                          '/export/share/applications/org.gnome.eog.desktop'],
                                         universal_newlines=True)
        lines = output.split('\n')
        assert 'Icon=org.gnome.eog' in lines
