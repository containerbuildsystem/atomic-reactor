%if 0%{?rhel} && 0%{?rhel} <= 6
%{!?__python2: %global __python2 /usr/bin/python2}
%{!?python2_sitelib: %global python2_sitelib %(%{__python2} -c "from distutils.sysconfig import get_python_lib; print(get_python_lib())")}
%{!?python2_sitearch: %global python2_sitearch %(%{__python2} -c "from distutils.sysconfig import get_python_lib; print(get_python_lib(1))")}
%endif

%if (0%{?fedora} >= 22 || 0%{?rhel} >= 8)
%global with_python3 1
%global binaries_py_version 3
%else
%global binaries_py_version 2
%endif

%global owner projectatomic
%global project atomic-reactor

%global commit 9b7fc79daa7aaa68286d042ef6667d10bfd55c2b
%global shortcommit %(c=%{commit}; echo ${c:0:7})

%global dock_obsolete_vr 1.3.7-2

Name:           %{project}
Version:        1.4.0
Release:        2%{?dist}

Summary:        Improved builder for Docker images
Group:          Development/Tools
License:        BSD
URL:            https://github.com/%{owner}/%{project}
Source0:        https://github.com/%{owner}/%{project}/archive/%{commit}/%{project}-%{commit}.tar.gz

BuildArch:      noarch

BuildRequires:  python2-devel
BuildRequires:  python-setuptools

%if 0%{?with_python3}
Requires:       python3-atomic-reactor
%else
Requires:       python-atomic-reactor
%endif
Requires:       git >= 1.7.10

%if 0%{?with_python3}
BuildRequires:  python3-devel
BuildRequires:  python3-setuptools
%endif

Provides:       dock = %{version}-%{release}
Obsoletes:      dock < %{dock_obsolete_vr}

%description
Simple Python tool with command line interface for building Docker
images. It contains a lot of helpful functions which you would
probably implement if you started hooking Docker into your
infrastructure.


%package -n python-atomic-reactor-koji
Summary:        Koji plugin for Atomic Reactor
Group:          Development/Tools
Requires:       python-atomic-reactor = %{version}-%{release}
Requires:       koji
Provides:       dock-koji = %{version}-%{release}
Provides:       python-dock-koji = %{version}-%{release}
Obsoletes:      dock-koji < 1.2.0-3
Obsoletes:      python-dock-koji < %{dock_obsolete_vr}

%description -n python-atomic-reactor-koji
Koji plugin for Atomic Reactor


%package -n python-atomic-reactor-metadata
Summary:        Plugin for submitting metadata to OSBS
Group:          Development/Tools
Requires:       python-atomic-reactor = %{version}-%{release}
Requires:       osbs
Provides:       dock-metadata = %{version}-%{release}
Provides:       python-dock-metadata = %{version}-%{release}
Obsoletes:      dock-metadata < 1.2.0-3
Obsoletes:      python-dock-metadata < %{dock_obsolete_vr}

%description -n python-atomic-reactor-metadata
Plugin for submitting metadata to OSBS


%package -n python-atomic-reactor
Summary:        Python 2 Atomic Reactor library
Group:          Development/Tools
License:        BSD
Requires:       python-docker-py
Requires:       python-requests
Requires:       python-setuptools
Requires:       python-dockerfile-parse
# Due to CopyBuiltImageToNFSPlugin, might be moved to subpackage later.
Requires:       nfs-utils
Provides:       python-dock = %{version}-%{release}
Obsoletes:      python-dock < %{dock_obsolete_vr}

%description -n python-atomic-reactor
Simple Python 2 library for building Docker images. It contains
a lot of helpful functions which you would probably implement if
you started hooking Docker into your infrastructure.


%if 0%{?with_python3}
%package -n python3-atomic-reactor
Summary:        Python 3 Atomic Reactor library
Group:          Development/Tools
License:        BSD
Requires:       python3-docker-py
Requires:       python3-requests
Requires:       python3-setuptools
Requires:       python3-dockerfile-parse
# Due to CopyBuiltImageToNFSPlugin, might be moved to subpackage later.
Requires:       nfs-utils
Provides:       python3-dock = %{version}-%{release}
Obsoletes:      python3-dock < %{dock_obsolete_vr}

%description -n python3-atomic-reactor
Simple Python 3 library for building Docker images. It contains
a lot of helpful functions which you would probably implement if
you started hooking Docker into your infrastructure.


%package -n python3-atomic-reactor-koji
Summary:        Koji plugin for Atomic Reactor
Group:          Development/Tools
Requires:       python3-atomic-reactor = %{version}-%{release}
Requires:       koji
Provides:       python3-dock-koji = %{version}-%{release}
Obsoletes:      python3-dock-koji < %{dock_obsolete_vr}

%description -n python3-atomic-reactor-koji
Koji plugin for Atomic Reactor


%package -n python3-atomic-reactor-metadata
Summary:        Plugin for submitting metadata to OSBS
Group:          Development/Tools
Requires:       python3-atomic-reactor = %{version}-%{release}
Requires:       osbs
Provides:       python3-dock-metadata = %{version}-%{release}
Obsoletes:      python3-dock-metadata < %{dock_obsolete_vr}

%description -n python3-atomic-reactor-metadata
Plugin for submitting metadata to OSBS
%endif # with_python3


%prep
%setup -qn %{name}-%{commit}


%build
# build python package
%{__python} setup.py build
%if 0%{?with_python3}
%{__python3} setup.py build
%endif # with_python3


%install
%if 0%{?with_python3}
%{__python3} setup.py install --skip-build --root %{buildroot}
mv %{buildroot}%{_bindir}/atomic-reactor %{buildroot}%{_bindir}/atomic-reactor3
mv %{buildroot}%{_bindir}/pulpsecret-gen %{buildroot}%{_bindir}/pulpsecret-gen3
%endif # with_python3

%{__python} setup.py install --skip-build --root %{buildroot}
mv %{buildroot}%{_bindir}/atomic-reactor %{buildroot}%{_bindir}/atomic-reactor2
ln -s %{_bindir}/atomic-reactor%{binaries_py_version} %{buildroot}%{_bindir}/atomic-reactor

mv %{buildroot}%{_bindir}/pulpsecret-gen %{buildroot}%{_bindir}/pulpsecret-gen2
ln -s %{_bindir}/pulpsecret-gen%{binaries_py_version} %{buildroot}%{_bindir}/pulpsecret-gen

# ship reactor in form of tarball so it can be installed within build image
cp -a %{sources} %{buildroot}/%{_datadir}/%{name}/atomic-reactor.tar.gz

mkdir -p %{buildroot}%{_mandir}/man1
cp -a docs/manpage/atomic-reactor.1 %{buildroot}%{_mandir}/man1/


%files
%doc README.md
%{_mandir}/man1/atomic-reactor.1*
%{!?_licensedir:%global license %%doc}
%license LICENSE
%{_bindir}/atomic-reactor
%{_bindir}/pulpsecret-gen

%files -n python-atomic-reactor
%doc README.md
%doc docs/*.md
%{!?_licensedir:%global license %%doc}
%license LICENSE
%{_bindir}/atomic-reactor2
%{_bindir}/pulpsecret-gen2
%dir %{python2_sitelib}/atomic_reactor
%{python2_sitelib}/atomic_reactor/*.*
%{python2_sitelib}/atomic_reactor/cli
%{python2_sitelib}/atomic_reactor/plugins
%exclude %{python2_sitelib}/atomic_reactor/plugins/pre_koji.py*
%exclude %{python2_sitelib}/atomic_reactor/plugins/post_store_metadata_in_osv3.py*
%{python2_sitelib}/atomic_reactor-%{version}-py2.*.egg-info
%dir %{_datadir}/%{name}
%{_datadir}/%{name}/atomic-reactor.tar.gz
%{_datadir}/%{name}/images


%files -n python-atomic-reactor-koji
%{python2_sitelib}/atomic_reactor/plugins/pre_koji.py*


%files -n python-atomic-reactor-metadata
%{python2_sitelib}/atomic_reactor/plugins/post_store_metadata_in_osv3.py*


%if 0%{?with_python3}
%files -n python3-atomic-reactor
%doc README.md
%doc docs/*.md
%{!?_licensedir:%global license %%doc}
%license LICENSE
%{_bindir}/atomic-reactor3
%{_bindir}/pulpsecret-gen3
%{_mandir}/man1/atomic-reactor.1*
%dir %{python3_sitelib}/atomic_reactor
%dir %{python3_sitelib}/atomic_reactor/__pycache__
%{python3_sitelib}/atomic_reactor/*.*
%{python3_sitelib}/atomic_reactor/cli
%{python3_sitelib}/atomic_reactor/plugins
%{python3_sitelib}/atomic_reactor/__pycache__/*.py*
%exclude %{python3_sitelib}/atomic_reactor/plugins/pre_koji.py*
%exclude %{python3_sitelib}/atomic_reactor/plugins/__pycache__/pre_koji*.py*
%exclude %{python3_sitelib}/atomic_reactor/plugins/__pycache__/post_store_metadata_in_osv3*.py*
%{python3_sitelib}/atomic_reactor-%{version}-py3.*.egg-info
%dir %{_datadir}/%{name}
# ship reactor in form of tarball so it can be installed within build image
%{_datadir}/%{name}/atomic-reactor.tar.gz
# dockerfiles for build images
# there is also a script which starts docker in privileged container
# (is not executable, because it's meant to be used within provileged containers, not on a host system)
%{_datadir}/%{name}/images


%files -n python3-atomic-reactor-koji
%{python3_sitelib}/atomic_reactor/plugins/pre_koji.py*
%{python3_sitelib}/atomic_reactor/plugins/__pycache__/pre_koji*.py*


%files -n python3-atomic-reactor-metadata
%{python3_sitelib}/atomic_reactor/plugins/post_store_metadata_in_osv3.py*
%{python3_sitelib}/atomic_reactor/plugins/__pycache__/post_store_metadata_in_osv3*.py*
%endif  # with_python3


%changelog
* Tue Jul 28 2015 bkabrda <bkabrda@redhat.com> - 1.4.0-2
- fix issues found during Fedora re-review (rhbz#1246702)

* Thu Jul 16 2015 Tomas Tomecek <ttomecek@redhat.com> - 1.4.0-1
- new upstream release 1.4.0

* Tue Jun 30 2015 Jiri Popelka <jpopelka@redhat.com> - 1.3.7-3
- define macros for RHEL-6

* Mon Jun 22 2015 Slavek Kabrda <bkabrda@redhat.com> - 1.3.7-2
- rename to atomic-reactor

* Mon Jun 22 2015 Martin Milata <mmilata@redhat.com> - 1.3.7-1
- new upstream release 1.3.7

* Wed Jun 17 2015 Jiri Popelka <jpopelka@redhat.com> - 1.3.6-2
- update hash

* Wed Jun 17 2015 Tomas Tomecek <ttomecek@redhat.com> - 1.3.6-1
- new upstream release 1.3.6

* Tue Jun 16 2015 Tomas Tomecek <ttomecek@redhat.com> - 1.3.5-1
- new upstream release 1.3.5

* Fri Jun 12 2015 Tomas Tomecek <ttomecek@redhat.com> - 1.3.4-1
- new upstream release 1.3.4

* Wed Jun 10 2015 Jiri Popelka <jpopelka@redhat.com> - 1.3.3-2
- BuildRequires:  python-docker-py

* Wed Jun 10 2015 Jiri Popelka <jpopelka@redhat.com> - 1.3.3-1
- new upstream release 1.3.3

* Mon Jun 01 2015 Tomas Tomecek <ttomecek@redhat.com> - 1.3.2-1
- new upstream release 1.3.2

* Wed May 27 2015 Tomas Tomecek <ttomecek@redhat.com> - 1.3.1-1
- new upstream release 1.3.1

* Mon May 25 2015 Tomas Tomecek <ttomecek@redhat.com> - 1.3.0-1
- new upstream release 1.3.0

* Tue May 19 2015 Jiri Popelka <jpopelka@redhat.com> - 1.2.1-3
- fix el7 build

* Tue May 19 2015 Jiri Popelka <jpopelka@redhat.com> - 1.2.1-2
- rebuilt

* Tue May 19 2015 Martin Milata <mmilata@redhat.com> - 1.2.1-1
- new upstream release 1.2.1

* Thu May 14 2015 Jiri Popelka <jpopelka@redhat.com> - 1.2.0-4
- enable Python 3 build

* Thu May 07 2015 Slavek Kabrda <bkabrda@redhat.com> - 1.2.0-3
- Introduce python-dock subpackage
- Rename dock-{koji,metadata} to python-dock-{koji,metadata}
- move /usr/bin/dock to /usr/bin/dock2, /usr/bin/dock is now a symlink

* Tue May 05 2015 Jiri Popelka <jpopelka@redhat.com> - 1.2.0-2
- require python[3]-setuptools

* Tue Apr 21 2015 Tomas Tomecek <ttomecek@redhat.com> - 1.2.0-1
- new upstream release 1.2.0

* Tue Apr 07 2015 Tomas Tomecek <ttomecek@redhat.com> - 1.1.3-1
- new upstream release 1.1.3

* Thu Apr 02 2015 Martin Milata <mmilata@redhat.com> - 1.1.2-1
- new upstream release 1.1.2

* Thu Mar 19 2015 Jiri Popelka <jpopelka@redhat.com> - 1.1.1-2
- separate executable for python 3

* Tue Mar 17 2015 Tomas Tomecek <ttomecek@redhat.com> - 1.1.1-1
- new upstream release 1.1.1

* Fri Feb 20 2015 Tomas Tomecek <ttomecek@redhat.com> - 1.1.0-1
- new upstream release 1.1.0

* Wed Feb 11 2015 Tomas Tomecek <ttomecek@redhat.com> - 1.0.0-2
- spec: fix python 3 packaging
- fix license in %%files
- comment on weird stuff (dock.tar.gz, docker.sh)

* Thu Feb 05 2015 Tomas Tomecek <ttomecek@redhat.com> - 1.0.0-1
- initial 1.0.0 upstream release

* Wed Feb 04 2015 Tomas Tomecek <ttomecek@redhat.com> 1.0.0.b-1
- new upstream release: beta

* Mon Dec 01 2014 Tomas Tomecek <ttomecek@redhat.com> 1.0.0.a-1
- complete rewrite (ttomecek@redhat.com)
- Use inspect_image() instead of get_image() when checking for existence (#4).
  (twaugh@redhat.com)

* Mon Nov 10 2014 Tomas Tomecek <ttomecek@redhat.com> 0.0.2-1
- more friendly error msg when build img doesnt exist (ttomecek@redhat.com)
- implement postbuild plugin system; do rpm -qa plugin (ttomecek@redhat.com)
- core, logs: wait for container to finish and then gather output
  (ttomecek@redhat.com)
- core, df copying: df was not copied when path wasn't provided
  (ttomecek@redhat.com)
- store dockerfile in results dir (ttomecek@redhat.com)

* Mon Nov 03 2014 Jakub Dorňák <jdornak@redhat.com> 0.0.1-1
- new package built with tito

* Sun Nov  2 2014 Jakub Dorňák <jdornak@redhat.com> - 0.0.1-1
- Initial package

