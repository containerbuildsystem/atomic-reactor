FROM fedora:latest
RUN dnf -y install python3-setuptools flatpak python3-pip git \
    gcc krb5-devel python3-devel popt-devel && dnf clean all
RUN mkdir /tmp/atomic-reactor
ADD . /tmp/atomic-reactor
RUN pip3 install git+https://github.com/containerbuildsystem/osbs-client
RUN cd /tmp/atomic-reactor && python3 setup.py install
CMD ["atomic-reactor", "--verbose", "inside-build"]
