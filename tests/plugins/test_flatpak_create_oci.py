"""
Copyright (c) 2017 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from six.moves import configparser
from flexmock import flexmock
import os
import pytest
import re
import shutil
import subprocess
import tarfile
from textwrap import dedent

from modulemd import ModuleMetadata

from atomic_reactor.constants import IMAGE_TYPE_OCI, IMAGE_TYPE_OCI_TAR
from atomic_reactor.inner import DockerBuildWorkflow
from atomic_reactor.plugin import PrePublishPluginsRunner
from atomic_reactor.plugins.prepub_flatpak_create_oci import FlatpakCreateOciPlugin
from atomic_reactor.plugins.pre_resolve_module_compose import (ModuleInfo,
                                                               ComposeInfo,
                                                               set_compose_info)
from atomic_reactor.plugins.pre_flatpak_create_dockerfile import (FlatpakSourceInfo,
                                                                  set_flatpak_source_info)
from atomic_reactor.util import ImageName

from tests.constants import TEST_IMAGE
from tests.fixtures import docker_tasker  # noqa
from tests.flatpak import (
    FLATPAK_APP_JSON, FLATPAK_APP_MODULEMD, FLATPAK_APP_FINISH_ARGS,
    FLATPAK_RUNTIME_JSON, FLATPAK_RUNTIME_MODULEMD
)

TEST_ARCH = 'x86_64'

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


default_check_output = subprocess.check_output
default_check_call = subprocess.check_call


# Instead of having <repo>/refs/<refname> pointing into <repo>/objects,
# just store the file tree at <repo>/<refname>
class MockOSTree:
    @staticmethod
    def commit(repo, branch, subject, tar_tree, dir_tree):
        branch_path = os.path.join(repo, branch)
        os.makedirs(branch_path)
        with tarfile.open(tar_tree) as tf:
            tf.extractall(path=branch_path)
        for f in os.listdir(dir_tree):
            full = os.path.join(dir_tree, f)
            if os.path.isdir(f):
                shutil.copytree(full, os.path.join(branch_path, f))
            else:
                shutil.copy2(full, os.path.join(branch_path, f))

    @staticmethod
    def init(repo):
        os.mkdir(repo)

    @staticmethod
    def summary(repo):
        pass


# The build directory is created more or less the same as flatpak build-init
# creates it, but when we 'flatpak build-export' we export to the fake
# OSTree format from MockOSTree, and when we 'flatpak build-bundle', we
# create a fake 'OCI Image' where we just have <dir>/tree with the filesystem
# contents, instead of having an index.json, tarred layers, etc.
class MockFlatpak:
    @staticmethod
    def default_arch():
        return TEST_ARCH

    @staticmethod
    def build_bundle(repo, filename, name, branch='master', runtime=False):
        if runtime:
            ref = 'runtime/' + name
        else:
            ref = 'app/' + name

        if branch is None:
            branch = os.listdir(os.path.join(repo, ref))[0]
        branch_path = os.path.join(repo, ref, TEST_ARCH, branch)
        dest_path = os.path.join(filename, 'tree')
        os.makedirs(filename)
        shutil.copytree(branch_path, dest_path)

    @staticmethod
    def build_init(directory, appname, sdk, runtime, runtime_branch):
        if not os.path.isdir(directory):
            os.mkdir(directory)
        with open(os.path.join(directory, "metadata"), "w") as f:
            f.write(dedent("""\
                           [Application]
                           name={appname}
                           runtime={runtime}/{arch}/{runtime_branch}
                           sdk={sdk}/{arch}/{runtime_branch}
                           """.format(appname=appname,
                                      sdk=sdk,
                                      runtime=runtime,
                                      runtime_branch=runtime_branch,
                                      arch=TEST_ARCH)))
        os.mkdir(os.path.join(directory, "files"))

    @staticmethod
    def build_finish(directory):
        pass

    @staticmethod
    def build_export(repo, directory):
        cp = configparser.RawConfigParser()
        cp.read(os.path.join(directory, "metadata"))
        appname = cp.get('Application', 'name')
        ref = os.path.join('app', appname, TEST_ARCH, 'master')

        dest = os.path.join(repo, ref)
        filesdir = os.path.join(directory, "files")
        shutil.copytree(filesdir, os.path.join(dest, "files"))
        shutil.copy2(os.path.join(directory, "metadata"), dest)

        # Simplified implementation of exporting files into /export
        # flatpak build-export only actually handles very specific files
        # desktop files in share/applications, icons, etc.
        dest_exportdir = os.path.join(dest, "export")
        for dirpath, dirname, filenames in os.walk(filesdir):
            rel_dirpath = os.path.relpath(dirpath, filesdir)
            for f in filenames:
                if f.startswith(appname):
                    destdir = os.path.join(dest_exportdir, rel_dirpath)
                    os.makedirs(destdir)
                    shutil.copy2(os.path.join(dirpath, f), destdir)


COMMAND_PATTERNS = [
    (['flatpak', '--default-arch'], MockFlatpak.default_arch),
    (['flatpak', 'build-bundle', '@repo',
      '--oci', '--runtime', '@filename', '@name', '@branch'],
     MockFlatpak.build_bundle, {'runtime': True}),
    (['flatpak', 'build-bundle', '@repo',
      '--oci', '@filename', '@name'],
     MockFlatpak.build_bundle),
    (['flatpak', 'build-export', '@repo', '@directory'],
     MockFlatpak.build_export),
    (['flatpak', 'build-finish'] + FLATPAK_APP_FINISH_ARGS + ['@directory'],
     MockFlatpak.build_finish),
    (['flatpak', 'build-init', '@directory', '@appname', '@sdk', '@runtime', '@runtime_branch'],
     MockFlatpak.build_init),
    (['ostree', 'commit',
      '--repo', '@repo',
      '--owner-uid=0', '--owner-gid=0', '--no-xattrs',
      '--branch', '@branch', '-s', '@subject', '--tree=tar=@tar_tree', '--tree=dir=@dir_tree'],
     MockOSTree.commit),
    (['ostree', 'init', '--mode=archive-z2', '--repo', '@repo'], MockOSTree.init),
    (['ostree', 'summary', '-u', '--repo', '@repo'], MockOSTree.summary)
]


def mock_command(cmdline, return_output=False, universal_newlines=False, cwd=None):
    output = ''
    cmd = cmdline[0]

    if cmd not in ('flatpak', 'ostree'):
        if output:
            return default_check_output(cmdline, universal_newlines=universal_newlines, cwd=cwd)
        else:
            return default_check_call(cmdline, cwd=cwd)

    for command in COMMAND_PATTERNS:
        if len(command) == 2:
            pattern, f = command
            default_args = {}
        else:
            pattern, f, default_args = command

        if len(pattern) != len(cmdline):
            continue

        matched = True
        kwargs = None
        for i, pattern_arg in enumerate(pattern):
            arg = cmdline[i]
            at_index = pattern_arg.find("@")
            if at_index < 0:
                if pattern_arg != arg:
                    matched = False
                    break
            else:
                before = pattern_arg[0:at_index]
                if not arg.startswith(before):
                    matched = False
                    break
                if kwargs is None:
                    kwargs = dict(default_args)
                kwargs[pattern_arg[at_index + 1:]] = arg[len(before):]

        if not matched:
            continue

        if kwargs is None:
            kwargs = dict(default_args)

        output = f(**kwargs)
        if output is None:
            output = ''

        if return_output:
            if universal_newlines:
                return output
            else:
                return output.encode('UTF-8')

    raise RuntimeError("Unmatched command line to mock %r" % cmdline)


def mocked_check_call(cmdline, cwd=None):
    mock_command(cmdline, return_output=True, cwd=cwd)


def mocked_check_output(cmdline, universal_newlines=False, cwd=None):
    return mock_command(cmdline, return_output=True, universal_newlines=universal_newlines, cwd=cwd)


class DefaultInspector(object):
    def __init__(self, tmpdir, metadata):
        # Import the OCI bundle into a ostree repository for examination
        self.repodir = os.path.join(str(tmpdir), 'repo')
        default_check_call(['ostree', 'init', '--mode=archive-z2', '--repo=' + self.repodir])
        default_check_call(['flatpak', 'build-import-bundle', '--oci',
                            self.repodir, str(metadata['path'])])

        self.ref_name = metadata['ref_name']

    def list_files(self):
        output = default_check_output(['ostree', '--repo=' + self.repodir,
                                       'ls', '-R', self.ref_name],
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

        return files

    def cat_file(self, path):
        return default_check_output(['ostree', '--repo=' + self.repodir,
                                     'cat', self.ref_name,
                                     path],
                                    universal_newlines=True)


class MockInspector(object):
    def __init__(self,  tmpdir, metadata):
        self.path = metadata['path']

    def list_files(self):
        def _make_absolute(path):
            if path.startswith("./"):
                return path[1:]
            else:
                return '/' + path

        files = []
        top = os.path.join(self.path, 'tree')
        for dirpath, dirname, filenames in os.walk(top):
            rel_dirpath = os.path.relpath(dirpath, top)
            files.extend([_make_absolute(os.path.join(rel_dirpath, f)) for f in filenames])

        return files

    def cat_file(self, path):
        full = os.path.join(self.path, 'tree', path[1:])
        with open(full, "r") as f:
            return f.read()


@pytest.mark.parametrize('config_name', ('app', 'runtime'))  # noqa - docker_tasker fixture
@pytest.mark.parametrize('mock_flatpak', (False, True))
def test_flatpak_create_oci(tmpdir, docker_tasker, config_name, mock_flatpak):
    if not mock_flatpak:
        # Check that we actually have flatpak available
        have_flatpak = False
        try:
            output = subprocess.check_output(['flatpak', '--version'],
                                             universal_newlines=True)
            m = re.search('(\d+)\.(\d+)\.(\d+)', output)
            if m and (int(m.group(1)), int(m.group(2)), int(m.group(3))) >= (0, 9, 7):
                have_flatpak = True

        except (subprocess.CalledProcessError, OSError):
            pass

        if not have_flatpak:
            return

    config = CONFIGS[config_name]

    if mock_flatpak:
        (flexmock(subprocess)
         .should_receive("check_call")
         .replace_with(mocked_check_call))

        (flexmock(subprocess)
         .should_receive("check_output")
         .replace_with(mocked_check_output))

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

    base_module = ModuleInfo(config['module_name'],
                             config['module_stream'],
                             config['module_version'],
                             mmd)
    repo_url = 'http://odcs.example/composes/latest-odcs-42-1/compose/Temporary/$basearch/os/'
    compose_info = ComposeInfo(42, base_module,
                               {config['module_name']: base_module},
                               repo_url)
    set_compose_info(workflow, compose_info)

    source = FlatpakSourceInfo(FLATPAK_APP_JSON,
                               compose_info)
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

    # Check that the expected files ended up in the flatpak

    if mock_flatpak:
        inspector = MockInspector(tmpdir, dir_metadata)
    else:
        inspector = DefaultInspector(tmpdir, dir_metadata)

    files = inspector.list_files()
    assert sorted(files) == config['expected_contents']

    if config_name is 'app':
        # Check that the desktop file was rewritten
        output = inspector.cat_file('/export/share/applications/org.gnome.eog.desktop')
        lines = output.split('\n')
        assert 'Icon=org.gnome.eog' in lines
