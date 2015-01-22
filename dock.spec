Name:           dock
Version:        1.0.0.a
Release:        1%{?dist}

Summary:        Improved builder for Docker images
Group:          Development Tools
License:        BSD
URL:            https://github.com/orgs/DBuildService/dock
Source0:        http://github.srcurl.net/DBuildService/%{name}/%{version}/%{name}-%{version}.tar.gz

BuildArch:      noarch

BuildRequires:  python-devel
BuildRequires:  python-setuptools

Requires:       python-docker-py
Requires:       GitPython

%description
Improved builder for Docker images


%package koji
Summary:        Koji plugin for Dock
Group:          Development Tools
Requires:       %{name} = %{version}-%{release}
Requires:       koji

%description koji
Koji plugin for Dock


%prep
%setup -q


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
%{python_sitelib}/dock/plugins/__init__.py*
%{python_sitelib}/dock/plugins/input_osv3.py*
%{python_sitelib}/dock/plugins/plugin_rpmqa.py*
%{python_sitelib}/dock/plugins/pre_cp_dockerfile.py*
%{python_sitelib}/dock/plugins/pre_inject_yum_repo.py*
%{python_sitelib}/dock/plugins/pre_return_dockerfile.py*
%{python_sitelib}/dock-%{version}-py2.*.egg-info
%dir %{_datadir}/%{name}
%{_datadir}/%{name}/dock.tar.gz
%{_datadir}/%{name}/images


%files koji
%{python_sitelib}/dock/plugins/pre_koji.py*


%changelog
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

