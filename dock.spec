Name:           dock
Version:        0.0.1
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


%description
Improved builder for Docker images


%prep
%setup -q


%build
# build python package
%{__python} setup.py build


%install
# install python package
%{__python} setup.py install --skip-build --root %{buildroot}


%files
%doc README.md
%{_bindir}/dock
%{python_sitelib}/dock
%{python_sitelib}/dock-%{version}-py2.*.egg-info


%changelog
* Mon Nov 03 2014 Jakub Dorňák <jdornak@redhat.com> 0.0.1-1
- new package built with tito

* Sun Nov  2 2014 Jakub Dorňák <jdornak@redhat.com> - 0.0.1-1
- Initial package

