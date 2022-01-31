#!/bin/bash
set -eux

# Prepare env vars
ENGINE=${ENGINE:="podman"}
OS=${OS:="centos"}
OS_VERSION=${OS_VERSION:="8"}
PYTHON_VERSION=${PYTHON_VERSION:="3"}
ACTION=${ACTION:="test"}
CONTAINER_NAME="atomic-reactor-$OS-$OS_VERSION-py$PYTHON_VERSION"

if [[ "$OS" == centos ]]; then
    IMAGE="quay.io/centos/centos:stream$OS_VERSION"
else
    IMAGE="$OS:$OS_VERSION"
fi

# Use arrays to prevent globbing and word splitting
engine_mounts=(-v "$PWD":"$PWD":z)
for dir in ${EXTRA_MOUNT:-}; do
  engine_mounts=("${engine_mounts[@]}" -v "$dir":"$dir":z)
done

# Create or resurrect container if needed
if [[ $($ENGINE ps -qa -f name="$CONTAINER_NAME" | wc -l) -eq 0 ]]; then
  $ENGINE run --name "$CONTAINER_NAME" -d "${engine_mounts[@]}" -w "$PWD" -ti "$IMAGE" sleep infinity
elif [[ $($ENGINE ps -q -f name="$CONTAINER_NAME" | wc -l) -eq 0 ]]; then
  echo found stopped existing container, restarting. volume mounts cannot be updated.
  $ENGINE container start "$CONTAINER_NAME"
fi

function setup_osbs() {
  # Optionally specify repo and branch for osbs-client to test changes
  # which depend on osbs-client patches not yet available in upstream master
  OSBS_CLIENT_REPO=${OSBS_CLIENT_REPO:-https://github.com/containerbuildsystem/osbs-client}
  OSBS_CLIENT_BRANCH=${OSBS_CLIENT_BRANCH:-osbs_ocp3}

  # PIP_PREFIX: osbs-client provides input templates that must be copied into /usr/share/...
  ENVS='-e PIP_PREFIX=/usr'
  RUN="$ENGINE exec -i ${ENVS} $CONTAINER_NAME"
  PYTHON="python$PYTHON_VERSION"
  PIP_PKG="$PYTHON-pip"
  PIP="pip$PYTHON_VERSION"
  PKG="dnf"
  PKG_EXTRA=(dnf-plugins-core desktop-file-utils flatpak ostree libmodulemd skopeo glibc-langpack-en "$PYTHON"-pylint)
  BUILDDEP=(dnf builddep)
  if [[ $OS == "centos" ]]; then
    ENABLE_REPO=
  else
    PKG_EXTRA+=("$PYTHON-libmodulemd")
    ENABLE_REPO="--enablerepo=updates-testing"
  fi

  # List common install dependencies
  PKG_COMMON_EXTRA=(git gcc krb5-devel python3-devel popt-devel)
  PKG_EXTRA+=("${PKG_COMMON_EXTRA[@]}")

  PIP_INST=("$PIP" install --index-url "${PYPI_INDEX:-https://pypi.org/simple}")

  if [[ $OS == "centos" ]]; then
    # Don't let builddep enable *-source repos since they give 404 errors
    $RUN rm -f /etc/yum.repos.d/CentOS-Sources.repo
    # This has to run *before* we try installing anything from EPEL
    $RUN $PKG $ENABLE_REPO install -y epel-release
  fi

  # RPM install basic dependencies
  $RUN $PKG $ENABLE_REPO install -y "${PKG_EXTRA[@]}"
  # RPM install build dependencies for atomic-reactor
  $RUN "${BUILDDEP[@]}" -y atomic-reactor.spec
  if [[ $OS != "centos" ]]; then
    # RPM remove python-docker-py because docker-squash will pull
    # in the latest version from PyPI. Don't remove the dependencies
    # that it pulled in, to avoid having to rebuild them.
    $RUN $PKG remove -y --noautoremove python{,3}-docker{,-py}
  fi

  # Install package
  $RUN $PKG install -y $PIP_PKG

  # Upgrade pip to provide latest features for successful installation
  $RUN "${PIP_INST[@]}" --upgrade pip

  if [[ $OS == centos ]]; then
    # Pip install/upgrade setuptools. Older versions of setuptools don't understand the
    # environment markers used by docker-squash's requirements, also
    # CentOS needs to have setuptools updates to make pytest-cov work
    $RUN "${PIP_INST[@]}" --upgrade setuptools
  fi

  # Install other dependencies for tests

  # Install osbs-client dependencies based on specfile
  # from specified git source (default: upstream master)
  $RUN rm -rf /tmp/osbs-client
  $RUN git clone --depth 1 --single-branch \
      "${OSBS_CLIENT_REPO}" --branch "${OSBS_CLIENT_BRANCH}" /tmp/osbs-client
  # RPM install build dependencies for osbs-client
  $RUN "${BUILDDEP[@]}" -y /tmp/osbs-client/osbs-client.spec
  # Run pip install with '--no-deps' to avoid compilation
  # This would also ensure all the deps are specified in the spec
  $RUN "${PIP_INST[@]}" --upgrade --no-deps --force-reinstall \
      "git+${OSBS_CLIENT_REPO}@${OSBS_CLIENT_BRANCH}"
  # Pip install dockerfile-parse
  $RUN "${PIP_INST[@]}" --upgrade --no-deps --force-reinstall git+https://github.com/DBuildService/dockerfile-parse

  # install with RPM_PY_SYS=true to avoid error caused by installing on system python
  $RUN sh -c "RPM_PY_SYS=true ${PIP_INST[*]} rpm-py-installer"
  # Pip install docker-squash
  $RUN "${PIP_INST[@]}" docker-squash
  # Setuptools install atomic-reactor from source
  $RUN $PYTHON setup.py install

  # Pip install packages for unit tests
  $RUN "${PIP_INST[@]}" -r tests/requirements.txt
}

case ${ACTION} in
"test")
  setup_osbs
  TEST_CMD="coverage run --source=atomic_reactor -m pytest tests"
  ;;
"pylint")
  setup_osbs
  PACKAGES='atomic_reactor tests'
  TEST_CMD="${PYTHON} -m pylint ${PACKAGES}"
  ;;
"bandit")
  setup_osbs
  $RUN "${PIP_INST[@]}" bandit
  TEST_CMD="bandit-baseline -r atomic_reactor -ll -ii"
  ;;
*)
  echo "Unknown action: ${ACTION}"
  exit 2
  ;;
esac

# Run tests
# shellcheck disable=SC2086
$RUN ${TEST_CMD} "$@"

echo "To run tests again:"
echo "$RUN ${TEST_CMD}"
