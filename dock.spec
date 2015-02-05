%global owner DBuildService
%global project dock

%global commit b08141fab67c5c602f6f02340677c92af6fe5bfb
%global shortcommit %(c=%{commit}; echo ${c:0:7})

Name:           dock
Version:        1.0.0
Release:        1%{?dist}

Summary:        Improved builder for Docker images
Group:          Development/Tools
License:        BSD
URL:            https://github.com/DBuildService/dock
Source0:        https://github.com/%{owner}/%{project}/archive/%{commit}/%{project}-%{commit}.tar.gz

BuildArch:      noarch

BuildRequires:  python-devel
BuildRequires:  python-setuptools

Requires:       python-docker-py
Requires:       GitPython
Requires:       python-requests

%description
Improved builder for Docker images


%package koji
Summary:        Koji plugin for Dock
Group:          Development/Tools
Requires:       %{name} = %{version}-%{release}
Requires:       koji

%description koji
Koji plugin for Dock


%prep
%setup -qn %{name}-%{commit}


%build
# build python package
%{__python} setup.py build


%install
mkdir -vp %{buildroot}/%{_datadir}/%{name}
# install python package
%{__python} setup.py install --skip-build --root %{buildroot}
cp -a %{sources} %{buildroot}/%{_datadir}/%{name}/dock.tar.gz


%files
%doc README.md
%{_bindir}/dock
%{python_sitelib}/dock/*.*
%{python_sitelib}/dock/cli
%{python_sitelib}/dock/plugins
%exclude %{python_sitelib}/dock/plugins/pre_koji.py*
%{python_sitelib}/dock-%{version}-py2.*.egg-info
%dir %{_datadir}/%{name}
%{_datadir}/%{name}/dock.tar.gz
%{_datadir}/%{name}/images


%files koji
%{python_sitelib}/dock/plugins/pre_koji.py*


%changelog
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

