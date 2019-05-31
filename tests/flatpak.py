from __future__ import absolute_import

import yaml

from osbs.repo_utils import ModuleSpec

try:
    from atomic_reactor.plugins.pre_resolve_module_compose import (ComposeInfo,
                                                                   set_compose_info)
    from atomic_reactor.plugins.pre_flatpak_create_dockerfile import set_flatpak_source_info
    from flatpak_module_tools.flatpak_builder import FlatpakSourceInfo, ModuleInfo
    from gi.repository import Modulemd
    MODULEMD_AVAILABLE = True
except ImportError:
    MODULEMD_AVAILABLE = False

PLATFORM_MODULEMD = """
document: modulemd
version: 2
data:
  name: platform
  stream: f28
  version: 5
  context: 00000000
  summary: Fedora 29 traditional base
  description: >-
    Fedora 29 traditional base
  license:
    module:
    - MIT
  xmd:
    mbs:
      mse: TRUE
      commit: f28
      buildrequires: {}
      koji_tag: module-f28-build
      requires: {}
  dependencies:
  - {}
  profiles:
    buildroot:
      rpms:
      - bash
    srpm-buildroot:
      rpms:
      - bash
  buildopts:
    rpms: {}
"""

FLATPAK_APP_MODULEMD = """
document: modulemd
version: 2
data:
  name: eog
  stream: f28
  version: 20170629213428
  summary: Eye of GNOME Application Module
  description: The Eye of GNOME image viewer (eog) is the official image viewer for
    the GNOME desktop. It can view single image files in a variety of formats, as
    well as large image collections.
  license:
    module: [MIT]
  dependencies:
  - buildrequires:
      flatpak-runtime: [f28]
    requires:
      flatpak-runtime: [f28]
  profiles:
    default:
      rpms: [eog]
  components:
    rpms: {}
  xmd:
    mbs: OMITTED
"""

FLATPAK_APP_RPMS = [
    "eog-0:3.24.1-1.module_7b96ed10.src.rpm",
    "eog-0:3.24.1-1.module_7b96ed10.x86_64.rpm",
    "eog-debuginfo-0:3.24.1-1.module_7b96ed10.x86_64.rpm",
    "eog-devel-0:3.24.1-1.module_7b96ed10.x86_64.rpm",
    "eog-tests-0:3.24.1-1.module_7b96ed10.x86_64.rpm",
    "exempi-0:2.4.2-4.module_7b96ed10.src.rpm",
    "exempi-0:2.4.2-4.module_7b96ed10.x86_64.rpm",
    "exempi-debuginfo-0:2.4.2-4.module_7b96ed10.x86_64.rpm",
    "exempi-devel-0:2.4.2-4.module_7b96ed10.x86_64.rpm",
    "glade-0:3.20.0-3.module_7b96ed10.src.rpm",
    "glade-0:3.20.0-3.module_7b96ed10.x86_64.rpm",
    "glade-debuginfo-0:3.20.0-3.module_7b96ed10.x86_64.rpm",
    "glade-devel-0:3.20.0-3.module_7b96ed10.x86_64.rpm",
    "glade-libs-0:3.20.0-3.module_7b96ed10.x86_64.rpm",
    "libexif-0:0.6.21-11.module_7b96ed10.src.rpm",
    "libexif-0:0.6.21-11.module_7b96ed10.x86_64.rpm",
    "libexif-debuginfo-0:0.6.21-11.module_7b96ed10.x86_64.rpm",
    "libexif-devel-0:0.6.21-11.module_7b96ed10.x86_64.rpm",
    "libexif-doc-0:0.6.21-11.module_7b96ed10.x86_64.rpm",
    "libpeas-1:1.20.0-5.module_7b96ed10.src.rpm",
    "libpeas-1:1.20.0-5.module_7b96ed10.x86_64.rpm",
    "libpeas-debuginfo-1:1.20.0-5.module_7b96ed10.x86_64.rpm",
    "libpeas-devel-1:1.20.0-5.module_7b96ed10.x86_64.rpm",
    "libpeas-gtk-1:1.20.0-5.module_7b96ed10.x86_64.rpm",
    "libpeas-loader-python-0:1.20.0-5.module_7b96ed10.x86_64.rpm",
    "libpeas-loader-python3-0:1.20.0-5.module_7b96ed10.x86_64.rpm",
]

FLATPAK_APP_FINISH_ARGS = [
    "--filesystem=host",
    "--share=ipc",
    "--socket=x11",
    "--socket=wayland",
    "--socket=session-bus",
    "--filesystem=~/.config/dconf:ro",
    "--filesystem=xdg-run/dconf",
    "--talk-name=ca.desrt.dconf",
    "--env=DCONF_USER_CONFIG_DIR=.config/dconf"
]

FLATPAK_APP_CONTAINER_YAML = """
compose:
    modules:
    - eog:f28
flatpak:
    id: org.gnome.eog
    branch: stable
    # Test overriding the automatic "first executable in /usr/bin'
    command: eog2
    tags: ["Viewer"]
    copy-icon: true
    rename-desktop-file: eog.desktop
    rename-icon: eog
    finish-args: >
""" + "".join("        {}\n".format(a) for a in FLATPAK_APP_FINISH_ARGS)

FLATPAK_RUNTIME_MODULEMD = """
document: modulemd
version: 2
data:
  name: flatpak-runtime
  stream: f28
  version: 20170701152209
  summary: Flatpak Runtime
  description: Libraries and data files shared between applications
  api:
    rpms: [librsvg2, gnome-themes-standard, abattis-cantarell-fonts, rest, xkeyboard-config,
      adwaita-cursor-theme, python3-gobject-base, json-glib, zenity, gsettings-desktop-schemas,
      glib-networking, gobject-introspection, gobject-introspection-devel, flatpak-rpm-macros,
      python3-gobject, gvfs-client, colord-libs, flatpak-runtime-config, hunspell-en-GB,
      libsoup, glib2-devel, hunspell-en-US, at-spi2-core, gtk3, libXtst, adwaita-gtk2-theme,
      libnotify, adwaita-icon-theme, libgcab1, libxkbcommon, libappstream-glib, python3-cairo,
      gnome-desktop3, libepoxy, hunspell, libgusb, glib2, enchant, at-spi2-atk]
  dependencies:
  - buildrequires:
      platform: [f28]
    requires:
      platform: [f28]
  license:
    module: [MIT]
  profiles:
    buildroot:
      rpms: [flatpak-rpm-macros, flatpak-runtime-config]
    runtime:
      rpms: [libwayland-server, librsvg2, libX11, libfdisk, adwaita-cursor-theme,
        libsmartcols, popt, gdbm, libglvnd, openssl-libs, gobject-introspection, systemd,
        ncurses-base, lcms2, libpcap, crypto-policies, fontconfig, libacl, libwayland-cursor,
        libseccomp, gmp, jbigkit-libs, bzip2-libs, libunistring, freetype, nettle,
        libidn, python3-six, gtk2, gtk3, ca-certificates, libdrm, rest, lzo, libcap,
        gnutls, pango, util-linux, basesystem, p11-kit, libgcab1, iptables-libs, dbus,
        python3-gobject-base, cryptsetup-libs, krb5-libs, sqlite-libs, kmod-libs,
        libmodman, libarchive, enchant, libXfixes, systemd-libs, shared-mime-info,
        coreutils-common, libglvnd-glx, abattis-cantarell-fonts, cairo, audit-libs,
        libwayland-client, libpciaccess, sed, libgcc, libXrender, json-glib, libxshmfence,
        glib-networking, libdb, fedora-modular-repos, keyutils-libs, hwdata, glibc,
        libproxy, python3-pyparsing, device-mapper, libgpg-error, system-python, shadow-utils,
        libXtst, libstemmer, dbus-libs, libpng, cairo-gobject, libXau, pcre, python3-packaging,
        at-spi2-core, gawk, mesa-libglapi, libXinerama, adwaita-gtk2-theme, libX11-common,
        device-mapper-libs, python3-appdirs, libXrandr, bash, glibc-common, libselinux,
        elfutils-libs, libxkbcommon, libjpeg-turbo, libuuid, atk, acl, libmount, lz4-libs,
        ncurses, libgusb, glib2, python3, libpwquality, at-spi2-atk, libattr, libcrypt,
        gnome-themes-standard, libtiff, harfbuzz, libstdc++, libXcomposite, xkeyboard-config,
        libxcb, libnotify, systemd-pam, readline, libXxf86vm, python3-cairo, gtk-update-icon-cache,
        python3-pip, mesa-libEGL, zenity, python3-gobject, libXcursor, tzdata, gvfs-client,
        libverto, libblkid, cracklib, libusbx, libcroco, libdatrie, gdk-pixbuf2, libXi,
        qrencode-libs, python3-libs, graphite2, mesa-libwayland-egl, mesa-libGL, pixman,
        libXext, glibc-all-langpacks, info, grep, fedora-modular-release, setup, zlib,
        libtasn1, libepoxy, hunspell, libsemanage, python3-setuptools, fontpackages-filesystem,
        libsigsegv, hicolor-icon-theme, libxml2, expat, libgcrypt, emacs-filesystem,
        gsettings-desktop-schemas, chkconfig, xz-libs, mesa-libgbm, libthai, coreutils,
        colord-libs, libcap-ng, flatpak-runtime-config, elfutils-libelf, hunspell-en-GB,
        libsoup, pam, hunspell-en-US, jasper-libs, p11-kit-trust, avahi-libs, elfutils-default-yama-scope,
        libutempter, adwaita-icon-theme, ncurses-libs, libidn2, system-python-libs,
        libffi, libXdamage, libglvnd-egl, libXft, cups-libs, ustr, libcom_err, libappstream-glib,
        gnome-desktop3, gdk-pixbuf2-modules, libsepol, filesystem, gzip, mpfr]
    sdk:
      rpms: [gcc]
  components:
    rpms: {}
  xmd:
    flatpak:
      # This gives information about how to map this module into Flatpak terms
      # this is used when building application modules against this module.
      branch: f28
      runtimes: # Keys are profile names
        runtime:
          id: org.fedoraproject.Platform
          sdk: org.fedoraproject.Sdk
        sdk:
          id: org.fedoraproject.Sdk
          runtime: org.fedoraproject.Platform
    mbs: OMITTED
"""  # noqa

FLATPAK_RUNTIME_CONTAINER_YAML = """
compose:
    modules:
    - flatpak-runtime:f28
flatpak:
    id: org.fedoraproject.Platform
    component: flatpak-runtime-container
    branch: f28
    sdk: org.fedoraproject.Sdk
    cleanup-commands: >
        touch -d @0 /usr/share/fonts
        touch -d @0 /usr/share/fonts/*
        fc-cache -fs
"""

FLATPAK_SDK_CONTAINER_YAML = """
compose:
    modules:
    - flatpak-runtime:f28/sdk
flatpak:
    id: org.fedoraproject.Sdk
    name: flatpak-sdk
    component: flatpak-sdk-container
    branch: f28
    runtime: org.fedoraproject.Platform
    cleanup-commands: >
        touch -d @0 /usr/share/fonts
        touch -d @0 /usr/share/fonts/*
        fc-cache -fs
"""

APP_CONFIG = {
    'base_module': 'eog',
    'modules': {
        'eog': {
            'stream': 'f28',
            'version': '20170629213428',
            'metadata': FLATPAK_APP_MODULEMD,
            'rpms': FLATPAK_APP_RPMS,
        },
        'flatpak-runtime': {
            'stream': 'f28',
            'version': '20170701152209',
            'metadata': FLATPAK_RUNTIME_MODULEMD,
            'rpms': [],  # We don't use this currently
        },
        'platform': {
            'stream': 'f28',
            'version': '5',
            'metadata': PLATFORM_MODULEMD,
            'rpms': [],  # We don't use this currently
        },
    },
    'container_yaml': FLATPAK_APP_CONTAINER_YAML,
    'name': 'eog',
    'component': 'eog',
}

RUNTIME_CONFIG = {
    'base_module': 'flatpak-runtime',
    'modules': {
        'flatpak-runtime': {
            'stream': 'f28',
            'version': '20170629185228',
            'metadata': FLATPAK_RUNTIME_MODULEMD,
            'rpms': [],  # We don't use this currently
        },
        'platform': {
            'stream': 'f28',
            'version': '5',
            'metadata': PLATFORM_MODULEMD,
            'rpms': [],  # We don't use this currently
        },
    },
    'container_yaml': FLATPAK_RUNTIME_CONTAINER_YAML,
    'name': 'flatpak-runtime',
    'component': 'flatpak-runtime-container',
}

SDK_CONFIG = {
    'base_module': 'flatpak-runtime',
    'profile': 'sdk',
    'modules': {
        'flatpak-runtime': {
            'stream': 'f28',
            'version': '20170629185228',
            'metadata': FLATPAK_RUNTIME_MODULEMD,
            'rpms': [],  # We don't use this currently
        },
        'platform': {
            'stream': 'f28',
            'version': '5',
            'metadata': PLATFORM_MODULEMD,
            'rpms': [],  # We don't use this currently
        },
    },
    'container_yaml': FLATPAK_SDK_CONTAINER_YAML,
    'name': 'flatpak-sdk',
    'component': 'flatpak-sdk-container',
}


def build_flatpak_test_configs(extensions=None):
    configs = {
        'app': APP_CONFIG,
        'runtime': RUNTIME_CONFIG,
        'sdk': SDK_CONFIG,
    }

    extensions = extensions or {}
    for key, config in extensions.items():
        configs[key].update(config)

    return configs


def setup_flatpak_compose_info(workflow, config=None):
    config = APP_CONFIG if config is None else config
    modules = {}
    for name, module_config in config['modules'].items():
        mmd = Modulemd.Module.new_from_string(module_config['metadata'])
        modules[name] = ModuleInfo(name,
                                   module_config['stream'],
                                   module_config['version'],
                                   mmd,
                                   module_config['rpms'])

    repo_url = 'http://odcs.example/composes/latest-odcs-42-1/compose/Temporary/$basearch/os/'

    base_module = modules[config['base_module']]
    source_spec = base_module.name + ':' + base_module.stream

    if 'profile' in config:
        source_spec += '/' + config['profile']

    compose = ComposeInfo(source_spec,
                          42, base_module,
                          modules,
                          repo_url,
                          'unsigned',
                          False)
    set_compose_info(workflow, compose)

    return compose


def setup_flatpak_source_info(workflow, config=None):
    config = APP_CONFIG if config is None else config
    compose = setup_flatpak_compose_info(workflow, config)

    flatpak_yaml = yaml.safe_load(config['container_yaml'])['flatpak']

    module_spec = ModuleSpec.from_str(compose.source_spec)

    source = FlatpakSourceInfo(flatpak_yaml, compose.modules, compose.base_module,
                               module_spec.profile)
    set_flatpak_source_info(workflow, source)

    return source
