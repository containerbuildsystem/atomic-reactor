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
from textwrap import dedent

from atomic_reactor.constants import IMAGE_TYPE_OCI, IMAGE_TYPE_OCI_TAR
from atomic_reactor.inner import DockerBuildWorkflow
from atomic_reactor.plugin import PrePublishPluginsRunner, PluginFailedException
from atomic_reactor.plugins.pre_reactor_config import (ReactorConfigPlugin,
                                                       WORKSPACE_CONF_KEY,
                                                       ReactorConfig)
from osbs.utils import ImageName

from tests.constants import MOCK_SOURCE
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
APP_MANIFEST_CONTENTS = b"""eog;3.24.1;1.module_7b96ed10;x86_64;(none);42;sigmd5;1491914281;sigpgp;siggpg
exempi;2.4.2;4.module_7b96ed10;x86_64;(none);42;sigmd5;1491914281;sigpgp;siggpg
libexif;0.6.21;11.module_7b96ed10;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
libpeas;1.20.0;5.module_7b96ed10;x86_64;1;42;sigmd5;1491914281;sigpgp;siggpg
libpeas-gtk;1.20.0;5.module_7b96ed10;x86_64;1;42;sigmd5;0;42;1491914281;sigpgp;siggpg
abattis-cantarell-fonts;0.0.25;2.module_e15740c0;noarch;(none);42;sigmd5;1491914281;sigpgp;siggpg
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

RUNTIME_MANIFEST_CONTENTS = b"""abattis-cantarell-fonts;0.0.25;2.module_e15740c0;noarch;0;42;sigmd5;1491914281;sigpgp;siggpg
acl;2.2.52;13.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
adwaita-cursor-theme;3.24.0;2.module_e15740c0;noarch;0;42;sigmd5;1491914281;sigpgp;siggpg
adwaita-gtk2-theme;3.22.3;2.module_e15740c0;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
adwaita-icon-theme;3.24.0;2.module_e15740c0;noarch;0;42;sigmd5;1491914281;sigpgp;siggpg
atk;2.24.0;1.module_98c1823a;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
at-spi2-atk;2.24.1;1.module_e15740c0;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
at-spi2-core;2.24.1;1.module_e15740c0;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
audit-libs;2.7.3;1.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
avahi-libs;0.6.32;7.module_f9511cd3;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
basesystem;11;3.module_7e01f122;noarch;0;42;sigmd5;1491914281;sigpgp;siggpg
bash;4.4.11;2.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
bzip2-libs;1.0.6;22.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
ca-certificates;2017.2.11;5.module_7e01f122;noarch;0;42;sigmd5;1491914281;sigpgp;siggpg
cairo;1.14.10;1.module_f9511cd3;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
cairo-gobject;1.14.10;1.module_f9511cd3;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
chkconfig;1.9;1.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
colord-libs;1.3.5;1.module_e15740c0;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
coreutils;8.27;3.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
coreutils-common;8.27;3.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
cracklib;2.9.6;5.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
crypto-policies;20170330;3.git55b66da.module_82827beb;noarch;0;42;sigmd5;1491914281;sigpgp;siggpg
cryptsetup-libs;1.7.3;3.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
cups-libs;2.2.2;6.module_98c1823a;x86_64;1;42;sigmd5;1491914281;sigpgp;siggpg
dbus;1.11.10;2.module_7e01f122;x86_64;1;42;sigmd5;1491914281;sigpgp;siggpg
dbus-libs;1.11.10;2.module_7e01f122;x86_64;1;42;sigmd5;1491914281;sigpgp;siggpg
device-mapper;1.02.137;4.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
device-mapper-libs;1.02.137;4.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
elfutils-default-yama-scope;0.168;5.module_7e01f122;noarch;0;42;sigmd5;1491914281;sigpgp;siggpg
elfutils-libelf;0.168;5.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
elfutils-libs;0.168;5.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
emacs-filesystem;25.2;0.1.rc2.module_7e01f122;noarch;1;42;sigmd5;1491914281;sigpgp;siggpg
enchant;1.6.0;16.module_e15740c0;x86_64;1;42;sigmd5;1491914281;sigpgp;siggpg
expat;2.2.0;2.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
fedora-modular-release;26;4.module_bc43b454;noarch;0;42;sigmd5;1491914281;sigpgp;siggpg
fedora-modular-repos;26;0.1.module_bc43b454;noarch;0;42;sigmd5;1491914281;sigpgp;siggpg
filesystem;3.2;40.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
flatpak-runtime-config;27;3.module_e15740c0;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
fontconfig;2.12.1;4.module_f9511cd3;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
fontpackages-filesystem;1.44;18.module_f9511cd3;noarch;0;42;sigmd5;1491914281;sigpgp;siggpg
freetype;2.7.1;9.module_f9511cd3;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
gawk;4.1.4;3.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
gdbm;1.12;2.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
gdk-pixbuf2;2.36.6;1.module_98c1823a;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
gdk-pixbuf2-modules;2.36.6;1.module_98c1823a;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
glib2;2.52.2;3.module_e15740c0;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
glibc;2.25;4.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
glibc-all-langpacks;2.25;4.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
glibc-common;2.25;4.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
glib-networking;2.50.0;2.module_e15740c0;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
gmp;6.1.2;3.module_7e01f122;x86_64;1;42;sigmd5;1491914281;sigpgp;siggpg
gnome-desktop3;3.24.2;1.module_e15740c0;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
gnome-themes-standard;3.22.3;2.module_e15740c0;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
gnutls;3.5.10;1.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
gobject-introspection;1.52.1;1.module_e15740c0;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
graphite2;1.3.6;2.module_f9511cd3;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
grep;3.0;1.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
gsettings-desktop-schemas;3.24.0;1.module_e15740c0;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
gtk2;2.24.31;3.module_f9511cd3;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
gtk3;3.22.16;1.module_e15740c0;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
gtk-update-icon-cache;3.22.16;1.module_e15740c0;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
gvfs-client;1.32.1;1.module_e15740c0;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
gzip;1.8;2.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
harfbuzz;1.4.4;1.module_f9511cd3;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
hicolor-icon-theme;0.15;4.module_f9511cd3;noarch;0;42;sigmd5;1491914281;sigpgp;siggpg
hunspell;1.5.4;2.module_e15740c0;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
hunspell-en-GB;0.20140811.1;6.module_e15740c0;noarch;0;42;sigmd5;1491914281;sigpgp;siggpg
hunspell-en-US;0.20140811.1;6.module_e15740c0;noarch;0;42;sigmd5;1491914281;sigpgp;siggpg
hwdata;0.301;1.module_f9511cd3;noarch;0;42;sigmd5;1491914281;sigpgp;siggpg
info;6.3;2.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
iptables-libs;1.6.1;2.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
jasper-libs;2.0.12;1.module_f9511cd3;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
jbigkit-libs;2.1;6.module_98c1823a;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
json-glib;1.2.8;1.module_e15740c0;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
keyutils-libs;1.5.9;9.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
kmod-libs;24;1.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
krb5-libs;1.15;9.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
lcms2;2.8;3.module_f9511cd3;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
libacl;2.2.52;13.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
libappstream-glib;0.7.0;1.module_e15740c0;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
libarchive;3.2.2;3.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
libattr;2.4.47;18.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
libblkid;2.29.1;2.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
libcap;2.25;5.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
libcap-ng;0.7.8;3.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
libcom_err;1.43.4;2.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
libcroco;0.6.11;3.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
libcrypt;2.25;4.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
libdatrie;0.2.9;4.module_f9511cd3;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
libdb;5.3.28;17.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
libdrm;2.4.81;1.module_98c1823a;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
libepoxy;1.4.1;1.module_e15740c0;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
libfdisk;2.29.1;2.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
libffi;3.1;10.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
libgcab1;0.7;2.module_e15740c0;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
libgcc;7.0.1;0.15.module_191b5bc9;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
libgcrypt;1.7.6;2.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
libglvnd;0.2.999;17.20170308git8e6e102.module_f9511cd3;x86_64;1;42;sigmd5;1491914281;sigpgp;siggpg
libglvnd-egl;0.2.999;17.20170308git8e6e102.module_f9511cd3;x86_64;1;42;sigmd5;1491914281;sigpgp;siggpg
libglvnd-glx;0.2.999;17.20170308git8e6e102.module_f9511cd3;x86_64;1;42;sigmd5;1491914281;sigpgp;siggpg
libgpg-error;1.25;2.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
libgusb;0.2.10;1.module_e15740c0;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
libidn;1.33;2.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
libidn2;0.16;2.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
libjpeg-turbo;1.5.1;2.module_98c1823a;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
libmodman;2.0.1;13.module_f9511cd3;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
libmount;2.29.1;2.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
libnotify;0.7.7;2.module_e15740c0;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
libpcap;1.8.1;3.module_7e01f122;x86_64;14;42;sigmd5;1491914281;sigpgp;siggpg
libpciaccess;0.13.4;4.module_f9511cd3;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
libpng;1.6.28;2.module_7e01f122;x86_64;2;42;sigmd5;1491914281;sigpgp;siggpg
libproxy;0.4.15;1.module_f9511cd3;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
libpwquality;1.3.0;8.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
librsvg2;2.40.17;1.module_e15740c0;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
libseccomp;2.3.2;1.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
libselinux;2.6;6.module_98c1823a;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
libsemanage;2.6;2.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
libsepol;2.6;1.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
libsigsegv;2.11;1.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
libsmartcols;2.29.1;2.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
libsoup;2.58.1;2.module_e15740c0;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
libstdc++;7.0.1;0.15.module_191b5bc9;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
libstemmer;0;5.585svn.module_f9511cd3;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
libtasn1;4.10;2.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
libthai;0.1.25;2.module_f9511cd3;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
libtiff;4.0.8;1.module_f9511cd3;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
libunistring;0.9.7;1.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
libusbx;1.0.21;2.module_f9511cd3;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
libutempter;1.1.6;9.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
libuuid;2.29.1;2.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
libverto;0.2.6;7.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
libwayland-client;1.13.0;2.module_e15740c0;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
libwayland-cursor;1.13.0;2.module_e15740c0;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
libwayland-server;1.13.0;2.module_e15740c0;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
libX11;1.6.5;2.module_98c1823a;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
libX11-common;1.6.5;2.module_98c1823a;noarch;0;42;sigmd5;1491914281;sigpgp;siggpg
libXau;1.0.8;7.module_f9511cd3;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
libxcb;1.12;3.module_f9511cd3;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
libXcomposite;0.4.4;9.module_98c1823a;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
libXcursor;1.1.14;8.module_98c1823a;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
libXdamage;1.1.4;9.module_98c1823a;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
libXext;1.3.3;5.module_98c1823a;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
libXfixes;5.0.3;2.module_98c1823a;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
libXft;2.3.2;5.module_98c1823a;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
libXi;1.7.9;2.module_98c1823a;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
libXinerama;1.1.3;7.module_98c1823a;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
libxkbcommon;0.7.1;3.module_e15740c0;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
libxml2;2.9.4;2.module_f9511cd3;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
libXrandr;1.5.1;2.module_98c1823a;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
libXrender;0.9.10;2.module_98c1823a;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
libxshmfence;1.2;4.module_98c1823a;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
libXtst;1.2.3;2.module_e15740c0;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
libXxf86vm;1.1.4;4.module_f9511cd3;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
lz4-libs;1.7.5;3.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
lzo;2.08;9.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
mesa-libEGL;17.1.4;1.module_e15740c0;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
mesa-libgbm;17.1.4;1.module_e15740c0;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
mesa-libGL;17.1.4;1.module_e15740c0;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
mesa-libglapi;17.1.4;1.module_e15740c0;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
mesa-libwayland-egl;17.1.4;1.module_e15740c0;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
mpfr;3.1.5;3.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
ncurses;6.0;8.20170212.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
ncurses-base;6.0;8.20170212.module_7e01f122;noarch;0;42;sigmd5;1491914281;sigpgp;siggpg
ncurses-libs;6.0;8.20170212.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
nettle;3.3;2.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
openssl-libs;1.1.0e;1.module_7e01f122;x86_64;1;42;sigmd5;1491914281;sigpgp;siggpg
p11-kit;0.23.5;1.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
p11-kit-trust;0.23.5;1.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
pam;1.3.0;2.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
pango;1.40.6;1.module_f9511cd3;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
pcre;8.40;5.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
pixman;0.34.0;3.module_f9511cd3;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
popt;1.16;8.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
python3;3.6.0;21.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
python3-appdirs;1.4.0;10.module_7e01f122;noarch;0;42;sigmd5;1491914281;sigpgp;siggpg
python3-cairo;1.10.0;20.module_e15740c0;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
python3-gobject;3.24.1;1.module_e15740c0;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
python3-gobject-base;3.24.1;1.module_e15740c0;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
python3-libs;3.6.0;21.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
python3-packaging;16.8;4.module_7e01f122;noarch;0;42;sigmd5;1491914281;sigpgp;siggpg
python3-pip;9.0.1;7.module_7e01f122;noarch;0;42;sigmd5;1491914281;sigpgp;siggpg
python3-pyparsing;2.1.10;3.module_7e01f122;noarch;0;42;sigmd5;1491914281;sigpgp;siggpg
python3-setuptools;36.0.1;1.module_e15740c0;noarch;0;42;sigmd5;1491914281;sigpgp;siggpg
python3-six;1.10.0;8.module_e15740c0;noarch;0;42;sigmd5;1491914281;sigpgp;siggpg
qrencode-libs;3.4.2;7.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
readline;7.0;5.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
rest;0.8.0;2.module_e15740c0;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
sed;4.4;1.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
setup;2.10.5;2.module_7e01f122;noarch;0;42;sigmd5;1491914281;sigpgp;siggpg
shadow-utils;4.3.1;3.module_7e01f122;x86_64;2;42;sigmd5;1491914281;sigpgp;siggpg
shared-mime-info;1.8;2.module_f9511cd3;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
sqlite-libs;3.17.0;2.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
systemd;233;3.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
systemd-libs;233;3.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
systemd-pam;233;3.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
system-python;3.6.0;21.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
system-python-libs;3.6.0;21.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
tzdata;2016j;3.module_7e01f122;noarch;0;42;sigmd5;1491914281;sigpgp;siggpg
ustr;1.0.4;22.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
util-linux;2.29.1;2.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
xkeyboard-config;2.21;1.module_e15740c0;noarch;0;42;sigmd5;1491914281;sigpgp;siggpg
xz-libs;5.2.3;2.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
zenity;3.24.0;1.module_e15740c0;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
zlib;1.2.11;2.module_7e01f122;x86_64;0;42;sigmd5;1491914281;sigpgp;siggpg
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

SDK_MANIFEST_CONTENTS = b"""gcc;7.3.1;2.fc27;x86_64;(none);54142500;sigmd5;1517331292;sigpgp;siggpg
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
    workflow.plugin_workspace[ReactorConfigPlugin.key] = {}

    reactor_map = {
        'version': 1,
        'flatpak': {'metadata': flatpak_metadata},
    }

    workflow.plugin_workspace[ReactorConfigPlugin.key] = {
        WORKSPACE_CONF_KEY: ReactorConfig(reactor_map)
    }


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


@pytest.mark.skipif(not MODULEMD_AVAILABLE,  # noqa - docker_tasker fixture
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
def test_flatpak_create_oci(tmpdir, docker_tasker, user_params,
                            config_name, flatpak_metadata, breakage):
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

    workflow = DockerBuildWorkflow(source=MOCK_SOURCE)
    workflow.user_params.update(USER_PARAMS)
    setattr(workflow, 'builder', MockBuilder())
    workflow.builder.df_path = write_docker_file(config, str(tmpdir))
    setattr(workflow.builder, 'tasker', docker_tasker)

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
    else:
        assert breakage is None
        expected_exception = None

    filesystem_tar = os.path.join(filesystem_dir, 'tar')
    with open(filesystem_tar, "wb") as f:
        with tarfile.TarFile(fileobj=f, mode='w') as tf:
            for f in os.listdir(filesystem_dir):
                tf.add(os.path.join(filesystem_dir, f), f)

    export_stream = open(filesystem_tar, "rb")

    def stream_to_generator(s):
        while True:
            # Yield small chunks to test the StreamAdapter code better
            buf = s.read(100)
            if len(buf) == 0:
                return
            yield buf

    export_generator = stream_to_generator(export_stream)

    (flexmock(docker_tasker.tasker)
     .should_receive('export_container')
     .with_args(CONTAINER_ID)
     .and_return(export_generator))

    (flexmock(docker_tasker.tasker.d.wrapped)
     .should_receive('create_container')
     .with_args(workflow.image, command=["/bin/bash"])
     .and_return({'Id': CONTAINER_ID}))

    (flexmock(docker_tasker.tasker.d.wrapped)
     .should_receive('remove_container')
     .with_args(CONTAINER_ID, force=False))

    setup_flatpak_source_info(workflow, config)

    runner = PrePublishPluginsRunner(
        docker_tasker,
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
        assert workflow.builder.image_id == filesystem_image_id
        assert filesystem_image_id not in for_removal
        runner.run()
        for_removal = workflow.plugin_workspace['remove_built_image']['images_to_remove']
        assert re.match(r'^sha256:\w{64}$', workflow.builder.image_id)
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
        assert any(re.match(r'runtime=org.fedoraproject.Platform/.*/f28$', l)
                   for l in metadata_lines)
        assert any(re.match(r'sdk=org.fedoraproject.Sdk/.*/f28$', l)
                   for l in metadata_lines)

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

@pytest.mark.skipif(not MODULEMD_AVAILABLE,  # noqa - docker_tasker fixture
                    reason="libmodulemd not available")
def test_skip_plugin(caplog, docker_tasker, user_params):
    workflow = DockerBuildWorkflow(source=MOCK_SOURCE)
    workflow.user_params = {}
    setattr(workflow, 'builder', MockBuilder())

    runner = PrePublishPluginsRunner(
        docker_tasker,
        workflow,
        [{
            'name': FlatpakCreateOciPlugin.key,
            'args': {}
        }]
    )

    runner.run()

    assert 'not flatpak build, skipping plugin' in caplog.text
