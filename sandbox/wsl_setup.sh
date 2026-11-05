#!/bin/sh
# One-time setup of the WSL sandbox. Run from the project root on Windows:
#   wsl.exe -d Ubuntu -u root -- sh sandbox/wsl_setup.sh
# Installs valgrind and cppcheck, creates the agrun user and installs ag-run.
set -eu
here=$(cd "$(dirname "$0")" && pwd)

if [ "$(id -u)" != 0 ]; then
  echo "run this as root (wsl.exe -u root)" >&2
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends gcc libc6-dev valgrind cppcheck procps coreutils util-linux

if ! id agrun >/dev/null 2>&1; then
  useradd --system --no-create-home --shell /usr/sbin/nologin agrun
fi

# CRLF from a Windows checkout would break the shebang
tr -d '\r' < "$here/ag-run" > /usr/local/bin/ag-run
chown root:root /usr/local/bin/ag-run
chmod 755 /usr/local/bin/ag-run

/usr/local/bin/ag-run check
echo "WSL sandbox ready"
