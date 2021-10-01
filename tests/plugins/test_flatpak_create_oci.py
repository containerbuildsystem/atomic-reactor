"""
Copyright (c) 2017, 2019 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from flexmock import flexmock
from io import BytesIO
import json
import os
import png
import pytest
import re
import subprocess
import tarfile
import time
from textwrap import dedent

from atomic_reactor.constants import IMAGE_TYPE_OCI, IMAGE_TYPE_OCI_TAR, SUBPROCESS_MAX_RETRIES
from atomic_reactor.inner import DockerBuildWorkflow
from atomic_reactor.plugin import PrePublishPluginsRunner, PluginFailedException
from osbs.utils import ImageName

from tests.flatpak import (MODULEMD_AVAILABLE,
                           setup_flatpak_source_info, build_flatpak_test_configs)

if MODULEMD_AVAILABLE:
    from atomic_reactor.plugins.prepub_flatpak_create_oci import FlatpakCreateOciPlugin
    from gi.repository import Modulemd

CONTAINER_ID = 'CONTAINER-ID'

ROOT = '/var/tmp/flatpak-build'

USER_PARAMS = {'flatpak': True}

DESKTOP_FILE_CONTENTS = b"""[Desktop Entry]
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

# The list of RPMs inherited from the runtime is abbreviated; we just need one
# (abattis-cantarell-fonts) to check that they are properly ignored.
APP_MANIFEST_CONTENTS = b"""eog;3.24.1;1.module_7b96ed10;x86_64;(none);42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
exempi;2.4.2;4.module_7b96ed10;x86_64;(none);42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
libexif;0.6.21;11.module_7b96ed10;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
libpeas;1.20.0;5.module_7b96ed10;x86_64;1;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
libpeas-gtk;1.20.0;5.module_7b96ed10;x86_64;1;42;sigmd5;0;42;1491914281;sigpgp;siggpg;dsaheader;rsaheader
abattis-cantarell-fonts;0.0.25;2.module_e15740c0;noarch;(none);42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
"""

ICON = BytesIO()
# create minimal 256x256 RGBA PNG
png.Writer(256, 256, greyscale=False, alpha=True).write(ICON,
                                                        [[0 for _ in range(4 * 256)]
                                                         for _ in range(256)])

APP_FILESYSTEM_CONTENTS = {
    '/usr/bin/not_eog': b'SHOULD_IGNORE',
    ROOT + '/usr/bin/also_not_eog': b'SHOULD_IGNORE',
    ROOT + '/app/bin/eog': b'MY_PROGRAM',
    ROOT + '/app/share/applications/eog.desktop': DESKTOP_FILE_CONTENTS,
    ROOT + '/app/share/icons/hicolor/256x256/apps/eog.png': ICON.getvalue(),
    '/var/tmp/flatpak-build.rpm_qf': APP_MANIFEST_CONTENTS
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
    'expected_ref_name': 'app/org.gnome.eog/x86_64/stable',
    'filesystem_contents': APP_FILESYSTEM_CONTENTS,
    'expected_contents': EXPECTED_APP_FLATPAK_CONTENTS,
    'expected_components': ['eog'],
    'unexpected_components': ['abattis-cantarell-fonts'],
}

RUNTIME_MANIFEST_CONTENTS = b"""abattis-cantarell-fonts;0.0.25;2.module_e15740c0;noarch;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
acl;2.2.52;13.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
adwaita-cursor-theme;3.24.0;2.module_e15740c0;noarch;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
adwaita-gtk2-theme;3.22.3;2.module_e15740c0;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
adwaita-icon-theme;3.24.0;2.module_e15740c0;noarch;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
atk;2.24.0;1.module_98c1823a;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
at-spi2-atk;2.24.1;1.module_e15740c0;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
at-spi2-core;2.24.1;1.module_e15740c0;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
audit-libs;2.7.3;1.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
avahi-libs;0.6.32;7.module_f9511cd3;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
basesystem;11;3.module_7e01f122;noarch;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
bash;4.4.11;2.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
bzip2-libs;1.0.6;22.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
ca-certificates;2017.2.11;5.module_7e01f122;noarch;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
cairo;1.14.10;1.module_f9511cd3;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
cairo-gobject;1.14.10;1.module_f9511cd3;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
chkconfig;1.9;1.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
colord-libs;1.3.5;1.module_e15740c0;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
coreutils;8.27;3.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
coreutils-common;8.27;3.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
cracklib;2.9.6;5.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
crypto-policies;20170330;3.git55b66da.module_82827beb;noarch;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
cryptsetup-libs;1.7.3;3.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
cups-libs;2.2.2;6.module_98c1823a;x86_64;1;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
dbus;1.11.10;2.module_7e01f122;x86_64;1;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
dbus-libs;1.11.10;2.module_7e01f122;x86_64;1;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
device-mapper;1.02.137;4.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
device-mapper-libs;1.02.137;4.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
elfutils-default-yama-scope;0.168;5.module_7e01f122;noarch;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
elfutils-libelf;0.168;5.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
elfutils-libs;0.168;5.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
emacs-filesystem;25.2;0.1.rc2.module_7e01f122;noarch;1;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
enchant;1.6.0;16.module_e15740c0;x86_64;1;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
expat;2.2.0;2.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
fedora-modular-release;26;4.module_bc43b454;noarch;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
fedora-modular-repos;26;0.1.module_bc43b454;noarch;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
filesystem;3.2;40.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
flatpak-runtime-config;27;3.module_e15740c0;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
fontconfig;2.12.1;4.module_f9511cd3;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
fontpackages-filesystem;1.44;18.module_f9511cd3;noarch;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
freetype;2.7.1;9.module_f9511cd3;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
gawk;4.1.4;3.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
gdbm;1.12;2.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
gdk-pixbuf2;2.36.6;1.module_98c1823a;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
gdk-pixbuf2-modules;2.36.6;1.module_98c1823a;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
glib2;2.52.2;3.module_e15740c0;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
glibc;2.25;4.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
glibc-all-langpacks;2.25;4.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
glibc-common;2.25;4.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
glib-networking;2.50.0;2.module_e15740c0;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
gmp;6.1.2;3.module_7e01f122;x86_64;1;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
gnome-desktop3;3.24.2;1.module_e15740c0;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
gnome-themes-standard;3.22.3;2.module_e15740c0;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
gnutls;3.5.10;1.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
gobject-introspection;1.52.1;1.module_e15740c0;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
graphite2;1.3.6;2.module_f9511cd3;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
grep;3.0;1.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
gsettings-desktop-schemas;3.24.0;1.module_e15740c0;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
gtk2;2.24.31;3.module_f9511cd3;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
gtk3;3.22.16;1.module_e15740c0;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
gtk-update-icon-cache;3.22.16;1.module_e15740c0;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
gvfs-client;1.32.1;1.module_e15740c0;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
gzip;1.8;2.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
harfbuzz;1.4.4;1.module_f9511cd3;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
hicolor-icon-theme;0.15;4.module_f9511cd3;noarch;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
hunspell;1.5.4;2.module_e15740c0;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
hunspell-en-GB;0.20140811.1;6.module_e15740c0;noarch;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
hunspell-en-US;0.20140811.1;6.module_e15740c0;noarch;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
hwdata;0.301;1.module_f9511cd3;noarch;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
info;6.3;2.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
iptables-libs;1.6.1;2.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
jasper-libs;2.0.12;1.module_f9511cd3;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
jbigkit-libs;2.1;6.module_98c1823a;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
json-glib;1.2.8;1.module_e15740c0;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
keyutils-libs;1.5.9;9.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
kmod-libs;24;1.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
krb5-libs;1.15;9.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
lcms2;2.8;3.module_f9511cd3;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
libacl;2.2.52;13.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
libappstream-glib;0.7.0;1.module_e15740c0;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
libarchive;3.2.2;3.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
libattr;2.4.47;18.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
libblkid;2.29.1;2.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
libcap;2.25;5.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
libcap-ng;0.7.8;3.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
libcom_err;1.43.4;2.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
libcroco;0.6.11;3.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
libcrypt;2.25;4.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
libdatrie;0.2.9;4.module_f9511cd3;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
libdb;5.3.28;17.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
libdrm;2.4.81;1.module_98c1823a;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
libepoxy;1.4.1;1.module_e15740c0;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
libfdisk;2.29.1;2.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
libffi;3.1;10.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
libgcab1;0.7;2.module_e15740c0;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
libgcc;7.0.1;0.15.module_191b5bc9;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
libgcrypt;1.7.6;2.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
libglvnd;0.2.999;17.20170308git8e6e102.module_f9511cd3;x86_64;1;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
libglvnd-egl;0.2.999;17.20170308git8e6e102.module_f9511cd3;x86_64;1;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
libglvnd-glx;0.2.999;17.20170308git8e6e102.module_f9511cd3;x86_64;1;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
libgpg-error;1.25;2.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
libgusb;0.2.10;1.module_e15740c0;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
libidn;1.33;2.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
libidn2;0.16;2.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
libjpeg-turbo;1.5.1;2.module_98c1823a;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
libmodman;2.0.1;13.module_f9511cd3;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
libmount;2.29.1;2.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
libnotify;0.7.7;2.module_e15740c0;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
libpcap;1.8.1;3.module_7e01f122;x86_64;14;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
libpciaccess;0.13.4;4.module_f9511cd3;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
libpng;1.6.28;2.module_7e01f122;x86_64;2;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
libproxy;0.4.15;1.module_f9511cd3;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
libpwquality;1.3.0;8.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
librsvg2;2.40.17;1.module_e15740c0;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
libseccomp;2.3.2;1.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
libselinux;2.6;6.module_98c1823a;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
libsemanage;2.6;2.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
libsepol;2.6;1.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
libsigsegv;2.11;1.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
libsmartcols;2.29.1;2.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
libsoup;2.58.1;2.module_e15740c0;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
libstdc++;7.0.1;0.15.module_191b5bc9;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
libstemmer;0;5.585svn.module_f9511cd3;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
libtasn1;4.10;2.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
libthai;0.1.25;2.module_f9511cd3;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
libtiff;4.0.8;1.module_f9511cd3;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
libunistring;0.9.7;1.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
libusbx;1.0.21;2.module_f9511cd3;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
libutempter;1.1.6;9.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
libuuid;2.29.1;2.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
libverto;0.2.6;7.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
libwayland-client;1.13.0;2.module_e15740c0;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
libwayland-cursor;1.13.0;2.module_e15740c0;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
libwayland-server;1.13.0;2.module_e15740c0;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
libX11;1.6.5;2.module_98c1823a;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
libX11-common;1.6.5;2.module_98c1823a;noarch;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
libXau;1.0.8;7.module_f9511cd3;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
libxcb;1.12;3.module_f9511cd3;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
libXcomposite;0.4.4;9.module_98c1823a;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
libXcursor;1.1.14;8.module_98c1823a;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
libXdamage;1.1.4;9.module_98c1823a;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
libXext;1.3.3;5.module_98c1823a;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
libXfixes;5.0.3;2.module_98c1823a;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
libXft;2.3.2;5.module_98c1823a;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
libXi;1.7.9;2.module_98c1823a;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
libXinerama;1.1.3;7.module_98c1823a;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
libxkbcommon;0.7.1;3.module_e15740c0;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
libxml2;2.9.4;2.module_f9511cd3;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
libXrandr;1.5.1;2.module_98c1823a;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
libXrender;0.9.10;2.module_98c1823a;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
libxshmfence;1.2;4.module_98c1823a;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
libXtst;1.2.3;2.module_e15740c0;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
libXxf86vm;1.1.4;4.module_f9511cd3;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
lz4-libs;1.7.5;3.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
lzo;2.08;9.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
mesa-libEGL;17.1.4;1.module_e15740c0;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
mesa-libgbm;17.1.4;1.module_e15740c0;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
mesa-libGL;17.1.4;1.module_e15740c0;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
mesa-libglapi;17.1.4;1.module_e15740c0;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
mesa-libwayland-egl;17.1.4;1.module_e15740c0;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
mpfr;3.1.5;3.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
ncurses;6.0;8.20170212.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
ncurses-base;6.0;8.20170212.module_7e01f122;noarch;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
ncurses-libs;6.0;8.20170212.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
nettle;3.3;2.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
openssl-libs;1.1.0e;1.module_7e01f122;x86_64;1;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
p11-kit;0.23.5;1.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
p11-kit-trust;0.23.5;1.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
pam;1.3.0;2.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
pango;1.40.6;1.module_f9511cd3;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
pcre;8.40;5.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
pixman;0.34.0;3.module_f9511cd3;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
popt;1.16;8.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
python3;3.6.0;21.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
python3-appdirs;1.4.0;10.module_7e01f122;noarch;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
python3-cairo;1.10.0;20.module_e15740c0;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
python3-gobject;3.24.1;1.module_e15740c0;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
python3-gobject-base;3.24.1;1.module_e15740c0;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
python3-libs;3.6.0;21.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
python3-packaging;16.8;4.module_7e01f122;noarch;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
python3-pip;9.0.1;7.module_7e01f122;noarch;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
python3-pyparsing;2.1.10;3.module_7e01f122;noarch;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
python3-setuptools;36.0.1;1.module_e15740c0;noarch;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
qrencode-libs;3.4.2;7.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
readline;7.0;5.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
rest;0.8.0;2.module_e15740c0;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
sed;4.4;1.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
setup;2.10.5;2.module_7e01f122;noarch;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
shadow-utils;4.3.1;3.module_7e01f122;x86_64;2;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
shared-mime-info;1.8;2.module_f9511cd3;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
sqlite-libs;3.17.0;2.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
systemd;233;3.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
systemd-libs;233;3.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
systemd-pam;233;3.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
system-python;3.6.0;21.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
system-python-libs;3.6.0;21.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
tzdata;2016j;3.module_7e01f122;noarch;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
ustr;1.0.4;22.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
util-linux;2.29.1;2.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
xkeyboard-config;2.21;1.module_e15740c0;noarch;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
xz-libs;5.2.3;2.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
zenity;3.24.0;1.module_e15740c0;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
zlib;1.2.11;2.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg;dsaheader;rsaheader
"""

RUNTIME_FILESYSTEM_CONTENTS = {
    '/usr/bin/not_eog': b'SHOULD_IGNORE',
    ROOT + '/etc/passwd': b'SOME_CONFIG_FILE',
    ROOT + '/etc/shadow:0444': b'FUNNY_PERMISSIONS',
    ROOT + '/usr/bin/bash': b'SOME_BINARY',
    ROOT + '/usr/bin/mount:1755': b'SOME_SETUID_BINARY',
    ROOT + '/usr/lib64/libfoo.so.1.0.0': b'SOME_LIB',
    ROOT + '/usr/share/foo:0777': None,  # writeable directory
    '/var/tmp/flatpak-build.rpm_qf': RUNTIME_MANIFEST_CONTENTS,
}

EXPECTED_RUNTIME_FLATPAK_CONTENTS = [
    '/files/bin/bash',
    '/files/bin/mount',
    '/files/etc/passwd',
    '/files/etc/shadow',
    '/files/lib64/libfoo.so.1.0.0',
    '/metadata'
]

RUNTIME_CONFIG = {
    'expected_ref_name': 'runtime/org.fedoraproject.Platform/x86_64/f28',
    'filesystem_contents': RUNTIME_FILESYSTEM_CONTENTS,
    'expected_contents': EXPECTED_RUNTIME_FLATPAK_CONTENTS,
    'expected_components': ['abattis-cantarell-fonts'],
    'unexpected_components': [],
}

SDK_MANIFEST_CONTENTS = b"""gcc;7.3.1;2.fc27;x86_64;(none);54142500;sigmd5;1517331292;sigpgp;siggpg;dsaheader;rsaheader
"""

SDK_FILESYSTEM_CONTENTS = {
    ROOT + '/usr/bin/gcc': b'SOME_BINARY',
    '/var/tmp/flatpak-build.rpm_qf': SDK_MANIFEST_CONTENTS,
}

EXPECTED_SDK_FLATPAK_CONTENTS = [
    '/files/bin/gcc',
    '/metadata'
]

SDK_CONFIG = {
    'expected_ref_name': 'runtime/org.fedoraproject.Sdk/x86_64/f28',
    'filesystem_contents': SDK_FILESYSTEM_CONTENTS,
    'expected_contents': EXPECTED_SDK_FLATPAK_CONTENTS,
    'expected_components': ['gcc'],
    'unexpected_components': [],
}

CONFIGS = build_flatpak_test_configs({
    'app': APP_CONFIG,
    'runtime': RUNTIME_CONFIG,
    'sdk': SDK_CONFIG,
})


class MockSource(object):
    dockerfile_path = None
    path = None


class MockBuilder(object):
    def __init__(self):
        self.image_id = "xxx"
        self.source = MockSource()
        self.base_image = ImageName(repo="qwe", tag="asd")


def load_labels_and_annotations(metadata):
    def get_path(descriptor):
        digest = descriptor["digest"]
        assert digest.startswith("sha256:")
        return os.path.join(metadata['path'],
                            "blobs/sha256",
                            digest[len("sha256:"):])

    with open(os.path.join(metadata['path'], "index.json")) as f:
        index_json = json.load(f)
    with open(get_path(index_json["manifests"][0])) as f:
        manifest_json = json.load(f)
    with open(get_path(manifest_json["config"])) as f:
        config_json = json.load(f)

    return config_json["config"]["Labels"], manifest_json["annotations"]


class DefaultInspector(object):
    def __init__(self, tmpdir, metadata):
        # Import the OCI bundle into a ostree repository for examination
        self.repodir = os.path.join(str(tmpdir), 'repo')
        subprocess.check_call(['ostree', 'init', '--mode=archive-z2', '--repo=' + self.repodir])
        subprocess.check_call(['flatpak', 'build-import-bundle', '--oci',
                               self.repodir, str(metadata['path'])])

        self.ref_name = metadata['ref_name']

    def list_files(self):
        output = subprocess.check_output(['ostree', '--repo=' + self.repodir,
                                          'ls', '-R', self.ref_name],
                                         universal_newlines=True)
        files = []
        for line in output.split('\n'):
            line = line.strip()
            if line == '':
                continue
            perms, _, _, _, path = line.split()
            if perms.startswith('d'):  # A directory
                continue
            files.append(path)

        return files

    def cat_file(self, path):
        return subprocess.check_output(['ostree', '--repo=' + self.repodir,
                                        'cat', self.ref_name,
                                        path],
                                       universal_newlines=True)

    def get_file_perms(self, path):
        output = subprocess.check_output(['ostree', '--repo=' + self.repodir,
                                          'ls', '-R', self.ref_name, path],
                                         universal_newlines=True)
        for line in output.split('\n'):
            line = line.strip()
            if line == '':
                continue
            perms = line.split()[0]
            return perms


def make_and_store_reactor_config_map(workflow, flatpak_metadata):
    reactor_map = {
        'version': 1,
        'flatpak': {'metadata': flatpak_metadata},
    }

    workflow.conf.conf = reactor_map


def write_docker_file(config, tmpdir):
    df_path = os.path.join(tmpdir, "Dockerfile")
    base_module_name = config['base_module']
    base_module = config['modules'][base_module_name]
    with open(df_path, "w") as f:
        f.write(dedent("""\
                       FROM fedora:30

                       LABEL name="{name}"
                       LABEL com.redhat.component="{component}"
                       LABEL version="{stream}"
                       LABEL release="{version}"
                       """.format(name=config['name'],
                                  component=config['component'],
                                  stream=base_module['stream'],
                                  version=base_module['version'])))

    return df_path


@pytest.mark.skip(reason="plugin needs rework to get image content")
@pytest.mark.skipif(not MODULEMD_AVAILABLE,  # noqa
                    reason="libmodulemd not available")
@pytest.mark.parametrize('config_name, flatpak_metadata, breakage', [
    ('app', 'both', None),
    ('app', 'both', 'copy_error'),
    ('app', 'both', 'no_runtime'),
    ('app', 'annotations', None),
    ('app', 'labels', None),
    ('runtime', 'both', None),
    ('sdk', 'both', None),
])
def test_flatpak_create_oci(tmpdir, user_params, config_name, flatpak_metadata, breakage):
    # Check that we actually have flatpak available
    have_flatpak = False
    try:
        output = subprocess.check_output(['flatpak', '--version'],
                                         universal_newlines=True)
        m = re.search(r'(\d+)\.(\d+)\.(\d+)', output)
        if m and (int(m.group(1)), int(m.group(2)), int(m.group(3))) >= (0, 9, 7):
            have_flatpak = True

    except (subprocess.CalledProcessError, OSError):
        pytest.skip(msg='flatpak not available')

    if not have_flatpak:
        return

    # Check if we have skopeo
    try:
        subprocess.check_output(['skopeo', '--version'])
    except (subprocess.CalledProcessError, OSError):
        pytest.skip(msg='skopeo not available')

    config = CONFIGS[config_name]

    workflow = DockerBuildWorkflow(source=None)
    workflow.user_params.update(USER_PARAMS)
    df_path = write_docker_file(config, str(tmpdir))
    flexmock(workflow, df_path=df_path)

    #  Make a local copy instead of pushing oci to docker storage
    workflow.storage_transport = 'oci:{}'.format(str(tmpdir))

    make_and_store_reactor_config_map(workflow, flatpak_metadata)

    filesystem_dir = os.path.join(str(tmpdir), 'filesystem')
    os.mkdir(filesystem_dir)

    filesystem_contents = config['filesystem_contents']

    for path, contents in filesystem_contents.items():
        parts = path.split(':', 1)
        path = parts[0]
        mode = parts[1] if len(parts) == 2 else None

        fullpath = os.path.join(filesystem_dir, path[1:])
        parent_dir = os.path.dirname(fullpath)
        if not os.path.isdir(parent_dir):
            os.makedirs(parent_dir)

        if contents is None:
            os.mkdir(fullpath)
        else:
            with open(fullpath, 'wb') as f:
                f.write(contents)

        if mode is not None:
            os.chmod(fullpath, int(mode, 8))

    if breakage == 'no_runtime':
        # Copy the parts of the config we are going to change
        config = dict(config)
        config['modules'] = dict(config['modules'])
        config['modules']['eog'] = dict(config['modules']['eog'])

        module_config = config['modules']['eog']

        mmd = Modulemd.ModuleStream.read_string(module_config['metadata'], strict=True)
        mmd.clear_dependencies()
        mmd.add_dependencies(Modulemd.Dependencies())
        mmd_index = Modulemd.ModuleIndex.new()
        mmd_index.add_module_stream(mmd)
        module_config['metadata'] = mmd_index.dump_to_string()

        expected_exception = 'Failed to identify runtime module'
    elif breakage == 'copy_error':
        workflow.storage_transport = 'idontexist'
        expected_exception = 'CalledProcessError'
        # mock the time.sleep() call between skopeo retries, otherwise test would take too long
        flexmock(time).should_receive('sleep').times(SUBPROCESS_MAX_RETRIES)
    else:
        assert breakage is None
        expected_exception = None

    filesystem_tar = os.path.join(filesystem_dir, 'tar')
    with open(filesystem_tar, "wb") as f:
        with tarfile.TarFile(fileobj=f, mode='w') as tf:
            for f in os.listdir(filesystem_dir):
                tf.add(os.path.join(filesystem_dir, f), f)

    # export_stream = open(filesystem_tar, "rb")

    # def stream_to_generator(s):
    #     while True:
    #         # Yield small chunks to test the StreamAdapter code better
    #         buf = s.read(100)
    #         if len(buf) == 0:
    #             return
    #         yield buf

    # export_generator = stream_to_generator(export_stream)

    setup_flatpak_source_info(workflow, config)

    runner = PrePublishPluginsRunner(
        workflow,
        [{
            'name': FlatpakCreateOciPlugin.key,
            'args': {}
        }]
    )

    if expected_exception:
        with pytest.raises(PluginFailedException) as ex:
            runner.run()
        assert expected_exception in str(ex.value)
    else:
        # Check if run replaces image_id and marks filesystem image for removal
        filesystem_image_id = 'xxx'
        for_removal = workflow.plugin_workspace.get(
            'remove_built_image', {}).get('images_to_remove', [])
        assert workflow.image_id == filesystem_image_id
        assert filesystem_image_id not in for_removal
        runner.run()
        for_removal = workflow.plugin_workspace['remove_built_image']['images_to_remove']
        assert re.match(r'^sha256:\w{64}$', workflow.image_id)
        assert filesystem_image_id in for_removal

        dir_metadata = workflow.exported_image_sequence[-2]
        assert dir_metadata['type'] == IMAGE_TYPE_OCI

        tar_metadata = workflow.exported_image_sequence[-1]
        assert tar_metadata['type'] == IMAGE_TYPE_OCI_TAR

        # Check that the correct labels and annotations were written

        labels, annotations = load_labels_and_annotations(dir_metadata)

        if config_name == 'app':
            assert labels['name'] == 'eog'
            assert labels['com.redhat.component'] == 'eog'
            assert labels['version'] == 'f28'
            assert labels['release'] == '20170629213428'
        elif config_name == 'runtime':  # runtime
            assert labels['name'] == 'flatpak-runtime'
            assert labels['com.redhat.component'] == 'flatpak-runtime-container'
            assert labels['version'] == 'f28'
            assert labels['release'] == '20170701152209'
        else:
            assert labels['name'] == 'flatpak-sdk'
            assert labels['com.redhat.component'] == 'flatpak-sdk-container'
            assert labels['version'] == 'f28'
            assert labels['release'] == '20170701152209'

        if flatpak_metadata == 'annotations':
            assert annotations.get('org.flatpak.ref') == config['expected_ref_name']
            assert 'org.flatpak.ref' not in labels
        elif flatpak_metadata == 'labels':
            assert 'org.flatpak.ref' not in annotations
            assert labels.get('org.flatpak.ref') == config['expected_ref_name']
        elif flatpak_metadata == 'both':
            assert annotations.get('org.flatpak.ref') == config['expected_ref_name']
            assert labels.get('org.flatpak.ref') == config['expected_ref_name']

        # Check that the expected files ended up in the flatpak

        # Flatpak versions before 1.6 require annotations to be present, and Flatpak
        # versions 1.6 and later require labels to be present. Skip the remaining
        # checks unless we have both annotations and labels.
        if flatpak_metadata != 'both':
            return

        inspector = DefaultInspector(tmpdir, dir_metadata)

        files = inspector.list_files()
        assert sorted(files) == config['expected_contents']

        components = {c['name'] for c in workflow.image_components}  # noqa:E501; pylint: disable=not-an-iterable
        for n in config['expected_components']:
            assert n in components
        for n in config['unexpected_components']:
            assert n not in components

        metadata_lines = inspector.cat_file('/metadata').split('\n')
        assert any(re.match(r'runtime=org.fedoraproject.Platform/.*/f28$', line)
                   for line in metadata_lines)
        assert any(re.match(r'sdk=org.fedoraproject.Sdk/.*/f28$', line)
                   for line in metadata_lines)

        if config_name == 'app':
            # Check that the desktop file was rewritten
            output = inspector.cat_file('/export/share/applications/org.gnome.eog.desktop')
            lines = output.split('\n')
            assert 'Icon=org.gnome.eog' in lines

            assert 'name=org.gnome.eog' in metadata_lines
            assert 'tags=Viewer' in metadata_lines
            assert 'command=eog2' in metadata_lines
        elif config_name == 'runtime':  # runtime
            # Check that permissions have been normalized
            assert inspector.get_file_perms('/files/etc/shadow') == '-00644'
            assert inspector.get_file_perms('/files/bin/mount') == '-00755'
            assert inspector.get_file_perms('/files/share/foo') == 'd00755'

            assert 'name=org.fedoraproject.Platform' in metadata_lines
        else:  # SDK
            assert 'name=org.fedoraproject.Sdk' in metadata_lines

@pytest.mark.skipif(not MODULEMD_AVAILABLE,  # noqa
                    reason="libmodulemd not available")
def test_skip_plugin(caplog, user_params):
    workflow = DockerBuildWorkflow(source=None)
    workflow.user_params = {}

    runner = PrePublishPluginsRunner(
        workflow,
        [{
            'name': FlatpakCreateOciPlugin.key,
            'args': {}
        }]
    )

    runner.run()

    assert 'not flatpak build, skipping plugin' in caplog.text
